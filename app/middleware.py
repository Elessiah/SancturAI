import asyncio

import anyio
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.admin_messages.service import verify_internal
from app.errors import ApiError, error_response
from app.state import AppState

NO_STORE_PREFIXES = ("/api/", "/mcp")


class NoStoreMiddleware:
    """Cache-Control: no-store sur toute réponse /api/* et /mcp (phrase, token, contenu du vault...).
    ASGI pur : ne met pas en tampon les flux (SSE du MCP)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not (path == "/api" or path.startswith(NO_STORE_PREFIXES)):
            await self.app(scope, receive, send)
            return

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message = {**message, "headers": [*headers, (b"cache-control", b"no-store")]}
            await send(message)

        await self.app(scope, receive, _send)


def _is_internal(path: str) -> bool:
    return path == "/internal" or path.startswith("/internal/")


def _paths(scope: Scope) -> tuple[str, str]:
    """Chemin brut et chemin vu par le routeur (sans root_path, comme starlette.get_route_path) :
    un root_path configuré (--root-path) ne doit pas faire contourner la garde."""
    path: str = scope.get("path", "")
    root: str = scope.get("root_path", "")
    if root and path.startswith(root + "/"):
        return path, path[len(root):]
    return path, path


class InternalGuardMiddleware:
    """Garde de tout /internal (routes actuelles et futures), AVANT le routage : sans token valide ou avec
    X-Forwarded-For, la réponse est celle d'une route inexistante (ni 405, ni 422 de décodage du corps)."""

    def __init__(self, app: ASGIApp, state: AppState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and any(_is_internal(p) for p in _paths(scope)):
            try:
                verify_internal(Request(scope), self.state.settings)
            except ApiError as err:
                await error_response(err)(scope, receive, send)
                return
        await self.app(scope, receive, send)


KIB = 1024
MIB = 1024 * KIB
# Plafonds de corps par route, au plus juste du besoin réel (JSON échappé au pire : 6 octets par caractère,
# 12 pour un emoji en paire de substitution) :
# - écriture vault (PUT /api/vault/..., by-id) et /mcp (vault_write) : fichier de 5 Mo en base64 ≈ 6,7 Mo, ou
#   en utf8 échappé ≤ 3 × 5 Mo ; même valeur que nginx (client_max_body_size 16m) ;
# - forum : corps ≤ 32 Ko en octets, soit ≤ 8 192 emoji × 12 ≈ 96 Kio échappés, + titre ;
# - ailleurs (login, inscription, messages admin ≤ 4 100 car. × 12 ≈ 48 Kio, déplacements...) : 64 Kio.
MAX_UPLOAD_BYTES = 16 * MIB
FORUM_BODY_BYTES = 128 * KIB
DEFAULT_BODY_BYTES = 64 * KIB
# Budget global des corps en vol (REST + MCP), compté sur la longueur déclarée AVANT tout parsing.
# Conteneur 896 Mio, 1 worker : socle Python/FastAPI/SQLAlchemy/SDK MCP ≈ 150 Mio ; Argon2 2 × 64 Mio = 128 Mio ;
# 1 000 sessions MCP × ~60 Kio de tas (≈ 120 Mio de RSS) → ≈ 400 Mio, reste ≈ 490 Mio.
# json.loads d'un corps dense en objets ([{},{},…]) ≈ 24 × sa taille (mesuré), + le corps brut : 25 × 18 Mio
# ≈ 450 Mio < 490. 18 Mio = un corps maximal (16 Mio) + 2 Mio de petits corps simultanés.
BODY_BUDGET_BYTES = 18 * MIB
BODY_BUDGET_WAIT = 10.0  # attente bornée d'une place dans le budget, puis SERVER_BUSY
# Détention bornée d'une réservation : le corps parsé vit jusqu'à la fin de la réponse (SSE du MCP) ; sans
# borne, un client qui lit sa réponse au compte-gouttes (ou envoie son corps lentement, sans nginx) la garde.
BODY_HOLD = 60.0
BUSY_RETRY_AFTER = 5
assert MAX_UPLOAD_BYTES <= BODY_BUDGET_BYTES


def body_cap(method: str, path: str) -> int:
    if path == "/mcp" or path.startswith("/mcp/") or (method == "PUT" and path.startswith("/api/vault/")):
        return MAX_UPLOAD_BYTES
    if path.startswith("/api/forum/"):
        return FORUM_BODY_BYTES
    return DEFAULT_BODY_BYTES


class ByteBudget:
    def __init__(self, total: int) -> None:
        self.total = total
        self.used = 0
        self._freed = asyncio.Event()

    async def acquire(self, n: int, wait_seconds: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while self.used + n > self.total:
            self._freed.clear()
            try:
                await asyncio.wait_for(self._freed.wait(), deadline - loop.time())
            except TimeoutError:
                return False
        self.used += n  # sans await depuis le contrôle : pas de course
        return True

    def release(self, n: int) -> None:
        self.used -= n
        self._freed.set()


class BodyLimitMiddleware:
    """Couche ASGI la plus externe (avant routage et parsing JSON) : plafond de corps par route (Content-Length
    refusé d'emblée, corps chunked compté pendant la lecture) et budget global d'octets en vol, REST et MCP."""

    def __init__(self, app: ASGIApp, budget_bytes: int = BODY_BUDGET_BYTES) -> None:
        self.app = app
        self.budget = ByteBudget(budget_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cap = body_cap(scope["method"], _paths(scope)[1])
        headers = Headers(scope=scope)
        length = headers.get("content-length", "")
        chunked = not length and "transfer-encoding" in headers
        reserve = cap if chunked else int(length) if length.isdigit() else 0
        if reserve > cap:
            await _too_large(cap)(scope, receive, send)
            return
        if reserve == 0:  # sans corps (GET, flux SSE) : ni compté ni borné dans le temps
            await self.app(scope, receive, send)
            return
        if not await self.budget.acquire(reserve, BODY_BUDGET_WAIT):
            busy = ApiError("SERVER_BUSY", "too many request bodies in flight", retry_after=BUSY_RETRY_AFTER)
            await error_response(busy)(scope, receive, send)
            return
        try:
            if chunked:  # longueur inconnue : lu ici, en comptant, avant d'être confié à l'application
                try:
                    body = await _read_capped(receive, cap)
                except _BodyTooLarge:
                    await _too_large(cap)(scope, receive, send)
                    return
                except _ClientGone:
                    return
                self.budget.release(reserve - len(body))
                reserve = len(body)
                receive = _replay(body, receive)
            await self._held(scope, receive, send)
        finally:
            self.budget.release(reserve)

    async def _held(self, scope: Scope, receive: Receive, send: Send) -> None:
        started = False

        async def watched_send(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        # Annuler le transport MCP ne coupe que la réponse : l'outil s'exécute dans la tâche de session du SDK.
        with anyio.move_on_after(BODY_HOLD) as hold:
            await self.app(scope, receive, watched_send)
        if hold.cancelled_caught and not started:  # sinon : 500 texte brut d'uvicorn (aucune réponse émise)
            slow = ApiError("SERVER_BUSY", "request took too long", retry_after=BUSY_RETRY_AFTER)
            await error_response(slow)(scope, receive, send)


def _too_large(cap: int) -> Response:
    return error_response(ApiError("VALIDATION_ERROR", f"request body exceeds {cap} bytes for this route"))


class _BodyTooLarge(Exception):
    pass


class _ClientGone(Exception):
    pass


async def _read_capped(receive: Receive, cap: int) -> bytes:
    """Corps complet ; _BodyTooLarge dès que `cap` est dépassé (le reste n'est pas lu)."""
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise _ClientGone  # corps incomplet : jamais confié à l'application
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > cap:
            raise _BodyTooLarge
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    delivered = False

    async def replay() -> Message:
        nonlocal delivered
        if delivered:
            return await receive()  # au-delà : attente de la déconnexion (flux SSE)
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay
