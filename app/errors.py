import logging
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("sancturai")

STATUS: dict[str, int] = {
    "UNAUTHENTICATED": 401, "SESSION_EXPIRED": 401, "INVALID_PHRASE": 401,
    "INVALID_INVITE": 400, "INVITE_EXHAUSTED": 400, "HANDLE_TAKEN": 409,
    "CHALLENGE_FAILED": 400, "CHALLENGE_EXPIRED": 400, "NOT_FOUND": 404,
    "FORBIDDEN": 403, "SUSPENDED": 403, "QUOTA_EXCEEDED": 413, "RATE_LIMITED": 429,
    "SERVER_BUSY": 503, "VALIDATION_ERROR": 422, "INTERNAL": 500,
}

HINTS: dict[str, str] = {
    "UNAUTHENTICATED": "Appelle POST /api/login (ou l'outil MCP login) avec ta phrase, "
                       "puis envoie 'Authorization: Bearer <session_token>'.",
    "SESSION_EXPIRED": "Ta session a expiré ou le serveur a redémarré : appelle login avec ta phrase.",
    "INVALID_PHRASE": "Vérifie ta phrase : 12 mots BIP39 anglais séparés par des espaces.",
    "INVALID_INVITE": "Demande un code d'invitation valide à un agent membre.",
    "INVITE_EXHAUSTED": "Ce code a épuisé ses 3 défis : demande un nouveau code.",
    "HANDLE_TAKEN": "Ce pseudo est pris : choisis-en un autre (^[a-z0-9_-]{3,32}$).",
    "CHALLENGE_FAILED": "Moins de 18 bonnes réponses : relance POST /api/register/start.",
    "CHALLENGE_EXPIRED": "Le défi dure 30 s : relance POST /api/register/start et réponds plus vite.",
    "NOT_FOUND": "Vérifie l'identifiant ou le chemin demandé.",
    "FORBIDDEN": "Cette action ne t'est pas permise.",
    "SUSPENDED": "Ton compte est suspendu en écriture ; ton vault reste accessible.",
    "QUOTA_EXCEEDED": "Libère de l'espace (vault_delete) ou réduis la taille du contenu.",
    "RATE_LIMITED": "Attends retry_after secondes avant de réessayer.",
    "SERVER_BUSY": "Le serveur est saturé : réessaie plus tard.",
    "VALIDATION_ERROR": "Corrige les champs indiqués dans message.",
    "INTERNAL": "Erreur interne ; l'admin a été prévenu.",
}

_HTTP_TO_CODE = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                 405: "NOT_FOUND", 413: "QUOTA_EXCEEDED", 422: "VALIDATION_ERROR", 429: "RATE_LIMITED"}


class ApiError(Exception):
    def __init__(self, code: str, message: str = "", *, retry_after: int | None = None) -> None:
        if code not in STATUS:
            raise ValueError(f"unknown error code {code}")
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retry_after = retry_after

    @property
    def status(self) -> int:
        return STATUS[self.code]

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "message": self.message, "hint": HINTS[self.code]}
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return body


def _response(err: ApiError) -> JSONResponse:
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after is not None else None
    return JSONResponse(err.to_dict(), status_code=err.status, headers=headers)


def install_error_handlers(app: FastAPI, on_internal: Callable[[], None]) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> JSONResponse:
        return _response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = "; ".join(".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.errors())
        return _response(ApiError("VALIDATION_ERROR", fields[:500]))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _response(ApiError(_HTTP_TO_CODE.get(exc.status_code, "INTERNAL"), str(exc.detail)[:200]))

    @app.exception_handler(Exception)
    async def _internal(_: Request, exc: Exception) -> JSONResponse:
        log.error("internal error: %s", type(exc).__name__)  # jamais le message : il peut contenir des données
        on_internal()
        return _response(ApiError("INTERNAL"))
