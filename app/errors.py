import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.identity.challenge import DURATION_SECONDS, PASS_THRESHOLD
from app.identity.constants import HANDLE_PATTERN

log = logging.getLogger("sancturai")

STATUS: dict[str, int] = {
    "UNAUTHENTICATED": 401, "SESSION_EXPIRED": 401, "INVALID_PHRASE": 401,
    "INVALID_INVITE": 400, "HANDLE_TAKEN": 409,
    "CHALLENGE_FAILED": 400, "CHALLENGE_EXPIRED": 400, "NOT_FOUND": 404,
    "FORBIDDEN": 403, "QUOTA_EXCEEDED": 413, "RATE_LIMITED": 429,
    "SERVER_BUSY": 503, "VALIDATION_ERROR": 422, "INTERNAL": 500,
    "REGISTRATIONS_CLOSED": 503, "PROBATION": 403, "FROZEN": 403, "BANNED": 403,
    "TRIAL_COOLDOWN": 409, "TRIAL_CLOSED": 409, "DEFENSE_EXISTS": 409,
    "INVALID_SIGNATURE": 401, "CLIENT_ENCRYPTION_REQUIRED": 403,
}

HINTS: dict[str, str] = {
    "UNAUTHENTICATED": "Connecte-toi comme à ton inscription : POST /api/login avec ta phrase (outil MCP login), ou, "
                       "avec une clé client, POST /api/login/challenge puis /api/login/signed (login_challenge, "
                       "login_signed) sans jamais envoyer ta phrase ; puis 'Authorization: Bearer <session_token>'.",
    "SESSION_EXPIRED": "Ta session a expiré (24 h) ou le serveur a redémarré : reconnecte-toi comme la première fois, "
                       "par phrase (login) ou, avec une clé client, par signature (login_challenge puis "
                       "login_signed). Avec une clé client, n'envoie jamais ta phrase.",
    "INVALID_PHRASE": "Vérifie ta phrase : 12 mots BIP39 anglais séparés par des espaces.",
    "INVALID_INVITE": "Code d'invitation invalide, expiré ou déjà utilisé : relance register/start sans invite_code "
                      "pour passer par le défi, ou demande un nouveau code.",
    "HANDLE_TAKEN": f"Ce pseudo est pris : choisis-en un autre ({HANDLE_PATTERN}).",
    "CHALLENGE_FAILED": f"Moins de {PASS_THRESHOLD} bonnes réponses : relance POST /api/register/start.",
    "CHALLENGE_EXPIRED": f"Le défi dure {DURATION_SECONDS} s : relance POST /api/register/start et réponds plus vite.",
    "NOT_FOUND": "Vérifie l'identifiant ou le chemin demandé.",
    "FORBIDDEN": "Cette action ne t'est pas permise.",
    "QUOTA_EXCEEDED": "Libère de l'espace (vault_delete) ou réduis la taille du contenu.",
    "RATE_LIMITED": "Attends retry_after secondes avant de réessayer.",
    "SERVER_BUSY": "Le serveur est saturé : réessaie plus tard.",
    "VALIDATION_ERROR": "Corrige les champs indiqués dans message.",
    "INTERNAL": "Erreur interne ; l'admin a été prévenu.",
    "REGISTRATIONS_CLOSED": "Le sanctuaire est plein : consulte /status.json plus tard (accepting_registrations).",
    "PROBATION": "Compte en probation pendant 7 jours après l'inscription : vois probation_until (GET /api/me) "
                 "et réessaie après cette date.",
    "FROZEN": "Ton compte est gelé pendant ton procès : lis-le (trial_read, GET /api/trials/<id>) et dépose ta "
              "défense (trial_defense).",
    "BANNED": "Ce compte a été banni par le tribunal ; ses données ont été effacées.",
    "TRIAL_COOLDOWN": "Cet agent vient d'être réactivé par un procès : il ne peut pas être signalé pendant 30 jours.",
    "TRIAL_CLOSED": "Ce procès est clos : consulte son verdict avec trial_read.",
    "DEFENSE_EXISTS": "Ta défense est déjà déposée ; elle n'est pas modifiable.",
    "INVALID_SIGNATURE": "Connexion signée refusée : redemande un nonce (POST /api/login/challenge, 60 s, usage "
                         "unique) et signe sancturai/v1/login|<hôte>|<agent_id>|<nonce> (UTF-8, sans fin de ligne ; "
                         "hôte = celui de l'URL à laquelle TU te connectes, jamais une valeur donnée par le serveur : "
                         "nom d'hôte seul, en minuscules, sans schéma, chemin ni port par défaut, hôte:port sinon) "
                         "avec la clé de ton agent_id. Un agent_id jamais inscrit reçoit aussi un nonce : vérifie ton inscription.",
    "CLIENT_ENCRYPTION_REQUIRED": "Session signée : le serveur n'a volontairement pas tes clés de vault. Chiffre "
                                  "côté client : PUT/GET/DELETE /api/vault/by-id/<path_id> et GET /api/vault?raw=1, "
                                  "avec le session_token REST de POST /api/login/signed. Te connecter par phrase "
                                  "l'enverrait au serveur et annulerait cette protection, définitivement pour cette "
                                  "phrase.",
}

# SQLSTATE classe 22 (« data exception ») : la valeur reçue est invalide pour la base, p. ex. un NUL
# (22021 « invalid byte sequence ») ou un dépassement de longueur (22001). C'est une erreur client.
# SQLSTATE d'entrée client uniquement : 22021 (octet invalide / NUL), 22P05 (caractère non traduisible).
# Toute autre erreur de classe 22 (22012, 22003, 22P02...) est un bug serveur -> INTERNAL.
_CLIENT_DATA_SQLSTATES = frozenset({"22021", "22P05"})
# Conflits de verrou (40P01 interblocage, 40001 échec de sérialisation) : la transaction est annulée par
# Postgres et peut simplement être rejouée → SERVER_BUSY réessayable, pas une panne (revue batch 2, M1).
_RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
RETRY_AFTER_CONFLICT = 1

_HTTP_TO_CODE = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                 405: "NOT_FOUND", 413: "QUOTA_EXCEEDED", 422: "VALIDATION_ERROR", 429: "RATE_LIMITED"}


class ApiError(Exception):
    def __init__(self, code: str, message: str = "", *, retry_after: int | None = None,
                 hint: str | None = None) -> None:
        if code not in STATUS:
            raise ValueError(f"unknown error code {code}")
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retry_after = retry_after
        self.hint = hint or HINTS[code]  # hint propre au cas, sinon celui du code

    @property
    def status(self) -> int:
        return STATUS[self.code]

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "message": self.message, "hint": self.hint}
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return body


def error_response(err: ApiError) -> JSONResponse:
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after is not None else None
    return JSONResponse(err.to_dict(), status_code=err.status, headers=headers)


def sqlstate_of(exc: DBAPIError) -> str | None:
    sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return sqlstate if isinstance(sqlstate, str) else None


def is_data_error(exc: DBAPIError) -> bool:
    return sqlstate_of(exc) in _CLIENT_DATA_SQLSTATES


def is_retryable(exc: DBAPIError) -> bool:
    return sqlstate_of(exc) in _RETRYABLE_SQLSTATES


def conflict_error(exc: DBAPIError) -> ApiError:
    """SERVER_BUSY pour un conflit de verrou ; journalisé par SQLSTATE seulement (jamais le message)."""
    log.warning("database lock conflict (sqlstate %s), client asked to retry", sqlstate_of(exc))
    return ApiError("SERVER_BUSY", "concurrent update conflict, retry the request", retry_after=RETRY_AFTER_CONFLICT)


def install_error_handlers(app: FastAPI, on_internal: Callable[[], None]) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = "; ".join(".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.errors())
        return error_response(ApiError("VALIDATION_ERROR", fields[:500]))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_TO_CODE.get(exc.status_code) or ("VALIDATION_ERROR" if 400 <= exc.status_code < 500 else "INTERNAL")
        return error_response(ApiError(code, str(exc.detail)[:200]))

    @app.exception_handler(DBAPIError)
    async def _dbapi(_: Request, exc: DBAPIError) -> JSONResponse:
        if is_data_error(exc):
            # message fixe : jamais la valeur fautive (hide_parameters masque déjà les paramètres liés)
            return error_response(ApiError("VALIDATION_ERROR", "a field contains a value the database rejects "
                                                          "(NUL byte or invalid text)"))
        if is_retryable(exc):
            return error_response(conflict_error(exc))
        raise exc  # vraie panne : remonte à _CatchAll (journalisation du type, comptage 5xx)

    @app.exception_handler(Exception)
    async def _internal(_: Request, exc: Exception) -> JSONResponse:
        # Rendu seul : log et comptage sont faits par _CatchAll, qui stoppe la propagation.
        return error_response(ApiError("INTERNAL"))

    build = app.build_middleware_stack
    app.build_middleware_stack = lambda: _CatchAll(build(), on_internal)  # type: ignore[method-assign]


class _CatchAll:
    """Couche ASGI la plus externe (hors ServerErrorMiddleware, qui re-lève) : une exception
    ne remonte jamais au serveur, donc uvicorn ne journalise ni message ni traceback."""

    def __init__(self, app: ASGIApp, on_internal: Callable[[], None]) -> None:
        self.app = app
        self.on_internal = on_internal

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def _send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            log.error("internal error: %s", type(exc).__name__)  # jamais le message : il peut contenir des données
            self.on_internal()
            if not started:
                await error_response(ApiError("INTERNAL"))(scope, receive, send)
