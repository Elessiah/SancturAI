import base64
import binascii
import hashlib
import hmac
import secrets
import struct
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from app.errors import ApiError

# Plafond de sessions vivantes par agent : au-delà, la plus ancienne est révoquée
# (borne la mémoire face à des logins répétés).
MAX_SESSIONS_PER_AGENT = 10


# Nonces de connexion signée (spec connexion signée §4) : sans état, 60 s, usage unique.
NONCE_TTL_SECONDS = 60
# Nonces déjà utilisés retenus jusqu'à leur échéance : seule une connexion signée d'un agent INSCRIT en ajoute un
# (bornée par la limite login: et par une vraie signature), jamais une demande de nonce.
MAX_USED_NONCES = 100_000
_NONCE_BYTES = 32  # horodatage 8 o ‖ sel 8 o ‖ HMAC tronqué 16 o


@dataclass
class Session:
    agent_id: str
    # None pour une session signée (spec connexion signée §6) : le serveur n'a pas les clés du vault
    vault_key: bytes | None = field(repr=False)
    path_key: bytes | None = field(repr=False)
    expires_at: float  # horloge interne (clock)

    @property
    def signed(self) -> bool:
        return self.vault_key is None

    def server_keys(self) -> tuple[bytes, bytes]:
        """(vault_key, path_key) des routes qui chiffrent côté serveur ; refus pour une session signée."""
        if self.vault_key is None or self.path_key is None:
            raise ApiError("CLIENT_ENCRYPTION_REQUIRED", "signed session: the server holds no vault key")
        return self.vault_key, self.path_key


class SessionStore:
    """Sessions et clés vault en mémoire de processus UNIQUEMENT (spec §5.4)."""

    def __init__(self, ttl_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._mcp: dict[str, str] = {}  # mcp_session_id -> token
        self._mcp_by_token: dict[str, set[str]] = {}  # index inverse (révocation O(1))
        # session MCP dont le token a été révoqué/expiré -> échéance : SESSION_EXPIRED (comme REST), pas
        # UNAUTHENTICATED ; borné par les liaisons d'un TTL, purgé avec les sessions
        self._mcp_revoked: dict[str, float] = {}
        self._by_agent: dict[str, dict[str, None]] = {}  # agent -> tokens (ordre de création)

    def create(self, agent_id: str, vault_key: bytes | None, path_key: bytes | None) -> tuple[str, float]:
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
            self._mcp_revoked[mcp_id] = self._clock() + self._ttl

    def agent_ids(self) -> list[str]:
        """Agents ayant au moins une session vivante (balayage des bannis par la maintenance)."""
        return list(self._by_agent)

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
        self._mcp_revoked.pop(mcp_session_id, None)
        self._mcp[mcp_session_id] = token
        self._mcp_by_token.setdefault(token, set()).add(mcp_session_id)

    def token_for_mcp(self, mcp_session_id: str) -> str | None:
        token = self._mcp.get(mcp_session_id)
        if token is None or self.get(token) is None:
            self._mcp.pop(mcp_session_id, None)
            return None
        return token

    def drop_dead_mcp(self, is_live: Callable[[str], bool]) -> None:
        """Sessions MCP closes (DELETE, inactivité) : un token lié par login n'est joignable que par elles,
        donc ses clés sont effacées dès que toutes ses sessions MCP sont closes, au lieu de vivre jusqu'au TTL."""
        for mcp_id in [m for m in self._mcp if not is_live(m)]:
            token = self._mcp.pop(mcp_id)
            siblings = self._mcp_by_token.get(token)
            if siblings is not None:
                siblings.discard(mcp_id)
                if not siblings:
                    del self._mcp_by_token[token]
                    self.revoke(token)
        for mcp_id in [m for m in self._mcp_revoked if not is_live(m)]:
            del self._mcp_revoked[mcp_id]  # id mort : plus aucune requête ne peut le présenter

    def mcp_expired(self, mcp_session_id: str) -> bool:
        """La session MCP a-t-elle porté un token désormais révoqué ou expiré ?"""
        return self._mcp_revoked.get(mcp_session_id, 0.0) > self._clock()

    def clear(self) -> None:
        self._sessions.clear()
        self._mcp.clear()
        self._mcp_revoked.clear()
        self._mcp_by_token.clear()
        self._by_agent.clear()

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, s in self._sessions.items() if s.expires_at <= now]:
            self.revoke(token)
        for mcp_id in [m for m, deadline in self._mcp_revoked.items() if deadline <= now]:
            del self._mcp_revoked[mcp_id]


class LoginNonces:
    """Nonces de connexion signée SANS ÉTAT (revue sécu/perf cycle 1) : un nonce est l'horodatage d'émission, un sel
    et un HMAC (secret de processus) qui le lie à l'agent_id. En émettre ne coûte aucune mémoire : un flot de demandes
    (IPv6 tournantes) ne peut ni remplir une table ni bloquer les connexions des autres. Seuls les nonces utilisés par
    une connexion réussie sont retenus, jusqu'à leur échéance, pour l'usage unique. Le secret change à chaque
    démarrage : les nonces émis avant un redémarrage sont refusés, comme les sessions sont perdues. L'horodatage est
    décalé d'une valeur aléatoire par processus : un nonce ne révèle pas depuis quand la machine tourne."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, secret: bytes | None = None) -> None:
        self._clock = clock
        self._secret = secret or secrets.token_bytes(32)
        self._offset = secrets.randbelow(2**40) / 1000  # s, ajouté à l'horloge (monotonic = temps depuis le boot)
        # nonce décodé -> échéance ; OrderedDict : retirer en tête est O(1) (un dict garderait des cases mortes)
        self._used: OrderedDict[bytes, float] = OrderedDict()

    def _now(self) -> float:
        return self._clock() + self._offset

    def _mac(self, agent_id: str, head: bytes) -> bytes:
        return hmac.new(self._secret, b"sancturai/login-nonce|" + head + agent_id.encode(), hashlib.sha256).digest()[:16]

    def issue(self, agent_id: str) -> str:
        head = struct.pack(">Q", int(self._now() * 1000)) + secrets.token_bytes(8)
        return base64.urlsafe_b64encode(head + self._mac(agent_id, head)).decode().rstrip("=")

    def check(self, agent_id: str, nonce: str) -> bytes | None:
        """Clé d'usage unique d'un nonce émis pour cet agent, frais et pas encore utilisé ; None sinon.
        La clé est le nonce DÉCODÉ : deux écritures base64 d'un même nonce ne passent pas deux fois."""
        try:
            raw = base64.urlsafe_b64decode(nonce + "=" * (-len(nonce) % 4))
        except (binascii.Error, ValueError):
            return None
        if len(raw) != _NONCE_BYTES or not hmac.compare_digest(raw[16:], self._mac(agent_id, raw[:16])):
            return None
        age = self._now() - struct.unpack(">Q", raw[:8])[0] / 1000
        if not 0 <= age < NONCE_TTL_SECONDS or raw in self._used:
            return None
        return raw

    def mark_used(self, key: bytes) -> None:
        """À appeler juste après un `check` réussi, sans `await` entre les deux (pas de course entre requêtes)."""
        now = self._now()
        while self._used:  # purge par le début, O(1) par entrée retirée
            first = next(iter(self._used))
            if self._used[first] > now:
                break
            self._used.popitem(last=False)
        if len(self._used) >= MAX_USED_NONCES:
            raise ApiError("SERVER_BUSY", "too many signed logins in the last minute", retry_after=NONCE_TTL_SECONDS)
        self._used[key] = struct.unpack(">Q", key[:8])[0] / 1000 + NONCE_TTL_SECONDS

    def clear(self) -> None:
        self._used.clear()
