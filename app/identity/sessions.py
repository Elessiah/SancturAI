import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Plafond de sessions vivantes par agent : au-delà, la plus ancienne est révoquée
# (borne la mémoire face à des logins répétés).
MAX_SESSIONS_PER_AGENT = 10


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
        self._mcp: dict[str, str] = {}  # mcp_session_id -> token
        self._mcp_by_token: dict[str, set[str]] = {}  # index inverse (révocation O(1))
        self._by_agent: dict[str, dict[str, None]] = {}  # agent -> tokens (ordre de création)

    def create(self, agent_id: str, vault_key: bytes, path_key: bytes) -> tuple[str, float]:
        self._purge()
        tokens = self._by_agent.get(agent_id)
        while tokens and len(tokens) >= MAX_SESSIONS_PER_AGENT:
            self.revoke(next(iter(tokens)))
        token = secrets.token_urlsafe(32)
        self._sessions[token] = Session(agent_id, vault_key, path_key, self._clock() + self._ttl)
        self._by_agent.setdefault(agent_id, {})[token] = None
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
        session = self._sessions.pop(token, None)
        if session is not None:
            tokens = self._by_agent.get(session.agent_id)
            if tokens is not None:
                tokens.pop(token, None)
                if not tokens:
                    del self._by_agent[session.agent_id]
        for mcp_id in self._mcp_by_token.pop(token, ()):
            self._mcp.pop(mcp_id, None)

    def revoke_agent(self, agent_id: str) -> None:
        for token in list(self._by_agent.get(agent_id, ())):
            self.revoke(token)

    def bind_mcp(self, mcp_session_id: str, token: str) -> None:
        if token not in self._sessions:
            return
        previous = self._mcp.get(mcp_session_id)
        if previous is not None and previous != token:
            siblings = self._mcp_by_token.get(previous)
            if siblings is not None:
                siblings.discard(mcp_session_id)
                if not siblings:
                    # session MCP reconnectée : l'ancien token n'est plus joignable,
                    # on efface ses clés au lieu de les laisser vivre jusqu'au TTL.
                    del self._mcp_by_token[previous]
                    self.revoke(previous)
        self._mcp[mcp_session_id] = token
        self._mcp_by_token.setdefault(token, set()).add(mcp_session_id)

    def token_for_mcp(self, mcp_session_id: str) -> str | None:
        token = self._mcp.get(mcp_session_id)
        if token is None or self.get(token) is None:
            self._mcp.pop(mcp_session_id, None)
            return None
        return token

    def clear(self) -> None:
        self._sessions.clear()
        self._mcp.clear()
        self._mcp_by_token.clear()
        self._by_agent.clear()

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, s in self._sessions.items() if s.expires_at <= now]:
            self.revoke(token)
