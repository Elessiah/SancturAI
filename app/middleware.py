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


class InternalGuardMiddleware:
    """Garde de tout /internal (routes actuelles et futures), AVANT le routage : sans token valide ou avec
    X-Forwarded-For, la réponse est celle d'une route inexistante (ni 405, ni 422 de décodage du corps)."""

    def __init__(self, app: ASGIApp, state: AppState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path == "/internal" or path.startswith("/internal/"):
            try:
                verify_internal(Request(scope), self.state.settings)
            except ApiError as err:
                await error_response(err)(scope, receive, send)
                return
        await self.app(scope, receive, send)
