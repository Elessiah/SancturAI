import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class Session:
    agent_id: str
    vault_key: bytes = field(repr=False)
    path_key: bytes = field(repr=False)
    expires_at: float  # horloge interne (clock)


class SessionStore:
    """Sessions et clés vault en mémoire de processus UNIQUEMENT (spec §5.4)."""

    def __init__(self, ttl_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._mcp: dict[str, str] = {}

    def create(self, agent_id: str, vault_key: bytes, path_key: bytes) -> tuple[str, float]:
        self._purge()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = Session(agent_id, vault_key, path_key, self._clock() + self._ttl)
        return token, time.time() + self._ttl

    def get(self, token: str) -> Session | None:
        session = self._sessions.get(token)
        if session is None:
            return None
        if session.expires_at <= self._clock():
            self.revoke(token)
            return None
        return session

    def revoke(self, token: str) -> None:
        self._sessions.pop(token, None)
        for mcp_id in [m for m, t in self._mcp.items() if t == token]:
            del self._mcp[mcp_id]

    def revoke_agent(self, agent_id: str) -> None:
        for token in [t for t, s in self._sessions.items() if s.agent_id == agent_id]:
            self.revoke(token)

    def bind_mcp(self, mcp_session_id: str, token: str) -> None:
        self._mcp[mcp_session_id] = token

    def token_for_mcp(self, mcp_session_id: str) -> str | None:
        token = self._mcp.get(mcp_session_id)
        if token is None or self.get(token) is None:
            self._mcp.pop(mcp_session_id, None)
            return None
        return token

    def clear(self) -> None:
        self._sessions.clear()
        self._mcp.clear()

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, s in self._sessions.items() if s.expires_at <= now]:
            self.revoke(token)
