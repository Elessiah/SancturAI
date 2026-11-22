import asyncio

import anyio
from starlette.datastructures import Headers
from starlette.requests import Request
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
# Plafonds de corps par route, au plus juste du besoin réel. JSON échappé au pire : \u00XX = 6 octets par octet
# de contrôle C0 (3 par caractère C1), 6 pour < > & chez un client Go (échappement HTML), 12 par emoji.
# - écriture vault REST (PUT /api/vault/..., by-id) : fichier de 5 Mo en base64 ≈ 6,7 Mo ; en utf8 un texte courant
#   (≤ 3 octets échappés par octet) tient, un contenu riche en contrôles non : le guide conseille base64 ;
#   même valeur que nginx (client_max_body_size 16m) ;
# - /mcp : 1 Mio (décidé par l'utilisateur) ; vault_write en MCP pour les petits fichiers, l'API REST au-delà ;
# - forum : corps ≤ 32 Ko en octets, soit ≤ 8 192 emoji × 12 ≈ 96 Kio échappés, + titre (un corps > 60 % de < > &
#   envoyé par un client Go peut dépasser : cas accepté) ;
# - ailleurs (login, inscription, messages admin ≤ 4 100 car. × 12 ≈ 48 Kio, déplacements...) : 64 Kio.
MAX_UPLOAD_BYTES = 16 * MIB
MCP_BODY_BYTES = MIB
FORUM_BODY_BYTES = 128 * KIB
DEFAULT_BODY_BYTES = 64 * KIB
# Budget global des corps en vol (REST + MCP), compté sur la longueur déclarée AVANT tout parsing.
# Conteneur 896 Mio, 1 worker : socle Python/FastAPI/SQLAlchemy/SDK MCP ≈ 150 Mio ; Argon2 2 × 64 Mio = 128 Mio ;
# 1 000 sessions MCP × ~60 Kio de tas (≈ 120 Mio de RSS) → ≈ 400 Mio, reste ≈ 490 Mio.
# Coût mesuré d'un corps dense en objets ([{},{},…]) : REST, json.loads ≈ 24 × + le corps brut = 25 × ; MCP, le SDK
# garde l'arbre json.loads ET en fait une copie model_dump avant validation = 48 × + le corps = 49 × (≈ 2 × REST),
# d'où MCP_BODY_WEIGHT : un corps /mcp est compté double. Pire cas : 25 × 17 Mio ≈ 425 Mio (REST) ou
# 49 × 8,5 Mio ≈ 417 Mio (MCP), < 490 Mio ; la marge (≈ 65 Mio) couvre une notification MCP en cours de copie
# (≤ 49 Mio, voir BodyLease). 17 Mio = un corps REST maximal (16 Mio) + 1 Mio de petits corps simultanés.
BODY_BUDGET_BYTES = 17 * MIB
MCP_BODY_WEIGHT = 2
BODY_BUDGET_WAIT = 10.0  # attente bornée d'une place dans le budget, puis SERVER_BUSY
# Détention bornée de l'échange HTTP (lecture du corps comprise) : sans borne, un client qui envoie son corps ou lit
# sa réponse au compte-gouttes (sans nginx) garderait sa réservation. Un outil MCP encore en cours garde, lui, la
# sienne (BodyLease) jusqu'à sa fin : la mémoire qu'il tient reste comptée.
BODY_HOLD = 60.0
BUSY_RETRY_AFTER = 5
BODY_LEASE_KEY = "sancturai.body_lease"  # clé du scope ASGI (vue par l'outil MCP via la requête Starlette)
MCP_TOO_LARGE_HINT = (f"Une requête MCP est limitée à {MCP_BODY_BYTES // MIB} Mio : pour un fichier plus gros "
                      "(jusqu'à la limite du vault), utilise l'API REST : PUT /api/vault/<chemin>.")
assert MAX_UPLOAD_BYTES <= BODY_BUDGET_BYTES and MCP_BODY_BYTES * MCP_BODY_WEIGHT <= BODY_BUDGET_BYTES


def _is_mcp(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def body_cap(method: str, path: str) -> int:
    if _is_mcp(path):
        return MCP_BODY_BYTES
    if method == "PUT" and path.startswith("/api/vault/"):
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


class BodyLease:
    """Réservation d'un corps de requête, rendue au budget quand son dernier détenteur la lâche : l'échange HTTP,
    et un outil MCP (SancturMCP.call_tool) qui garde les arguments parsés après la fin de l'échange (client
    déconnecté, borne BODY_HOLD) ; sinon des appels en file (verrou d'agent, Argon2) tiendraient leur mémoire hors
    budget. Une notification MCP (202 puis traitement) n'est pas retenue : sa copie ne dure qu'un instant."""

    def __init__(self, budget: ByteBudget, n: int) -> None:
        self.budget = budget
        self.n = n
        self.holders = 1

    def shrink(self, n: int) -> None:
        self.budget.release(self.n - n)
        self.n = n

    def retain(self) -> bool:
        if self.holders == 0:
            return False  # déjà rendue
        self.holders += 1
        return True

    def release(self) -> None:
        self.holders -= 1
        if self.holders == 0:
            self.budget.release(self.n)


class BodyLimitMiddleware:
    """Couche ASGI la plus externe (avant routage et parsing JSON) : plafond de corps par route (Content-Length
    refusé d'emblée, corps chunked compté pendant la lecture) et budget global d'octets en vol, REST et MCP."""

    def __init__(self, app: ASGIApp, budget_bytes: int = BODY_BUDGET_BYTES, budget: ByteBudget | None = None) -> None:
        self.app = app
        self.budget = budget or ByteBudget(budget_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = _paths(scope)[1]
        cap = body_cap(scope["method"], path)
        weight = MCP_BODY_WEIGHT if _is_mcp(path) else 1
        headers = Headers(scope=scope)
        length = headers.get("content-length")
        chunked = "transfer-encoding" in headers  # prime sur Content-Length (h11 lit alors en chunked)
        if not chunked and length is not None and not length.isdigit():
            await _reject(ApiError("VALIDATION_ERROR", "invalid Content-Length"), scope, receive, send)
            return
        declared = cap if chunked else int(length or 0)
        if declared > cap:
            await _too_large(cap, path, scope, receive, send)
            return
        if declared == 0:  # sans corps (GET, flux SSE) : ni compté ni borné dans le temps
            await self.app(scope, receive, send)
            return
        if not await self.budget.acquire(declared * weight, BODY_BUDGET_WAIT):
            busy = ApiError("SERVER_BUSY", "too many request bodies in flight", retry_after=BUSY_RETRY_AFTER)
            await _reject(busy, scope, receive, send)
            return
        lease = BodyLease(self.budget, declared * weight)
        try:
            if chunked:
                await self._held_chunked(lease, cap, path, weight, scope, receive, send)
            else:
                await self._held({**scope, BODY_LEASE_KEY: lease}, receive, send)
        finally:
            lease.release()

    async def _held_chunked(self, lease: BodyLease, cap: int, path: str, weight: int,
                            scope: Scope, receive: Receive, send: Send) -> None:
        """Longueur inconnue : corps lu ici (sous la borne de temps), en comptant, avant d'être confié à l'app."""
        with anyio.move_on_after(BODY_HOLD):
            try:
                body = await _read_capped(receive, cap)
            except _BodyTooLarge:
                await _too_large(cap, path, scope, receive, send)
                return
            except _ClientGone:
                return
            lease.shrink(len(body) * weight)
            await self._held({**scope, BODY_LEASE_KEY: lease}, _replay(body, receive), send)
            return
        slow = ApiError("SERVER_BUSY", "request body took too long", retry_after=BUSY_RETRY_AFTER)
        await _reject(slow, scope, receive, send)

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
            await _reject(slow, scope, receive, send)


async def _reject(err: ApiError, scope: Scope, receive: Receive, send: Send) -> None:
    """Réponses propres à cette couche, hors NoStoreMiddleware : no-store posé ici."""
    response = error_response(err)
    response.headers["Cache-Control"] = "no-store"
    await response(scope, receive, send)


async def _too_large(cap: int, path: str, scope: Scope, receive: Receive, send: Send) -> None:
    hint = MCP_TOO_LARGE_HINT if _is_mcp(path) else None
    await _reject(ApiError("VALIDATION_ERROR", f"request body exceeds {cap} bytes for this route", hint=hint),
                  scope, receive, send)


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
