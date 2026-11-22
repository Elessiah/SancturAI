import ipaddress
from collections.abc import Mapping

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.auth import ensure_writable, load_agent, resolve_session
from app.identity.models import Agent
from app.identity.sessions import Session
from app.state import AppState, get_db, get_state

_bearer = HTTPBearer(auto_error=False)


def normalize_ip(raw: str) -> str:
    """Clé de limitation par client : IPv4 par adresse, IPv6 par /64 (un client y tourne librement)."""
    try:
        ip = ipaddress.ip_address(raw.strip())
    except ValueError:
        return "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def client_ip_from_headers(headers: Mapping[str, str], fallback: str = "") -> str:
    """Clé IP commune à REST et MCP (mêmes buckets `register:` / `login:` pour les deux transports)."""
    return normalize_ip(headers.get("x-real-ip") or fallback)


def client_ip(request: Request) -> str:
    return client_ip_from_headers(request.headers, request.client.host if request.client else "")


async def current_token(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str | None:
    return creds.credentials if creds else None


async def current_session(token: str | None = Depends(current_token),
                          state: AppState = Depends(get_state)) -> Session:
    return resolve_session(state, token)


async def uncounted_session(token: str | None = Depends(current_token),
                            state: AppState = Depends(get_state)) -> Session:
    return resolve_session(state, token, count=False)


async def member_session(session: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
                         state: AppState = Depends(get_state)) -> Session:
    """Session d'un agent ni gelé ni banni (vault : les routes travaillent sur la session, statut lu en base)."""
    await load_agent(db, state, session)
    return session


async def current_agent(session: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
                        state: AppState = Depends(get_state)) -> Agent:
    return await load_agent(db, state, session)


async def any_agent(session: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
                    state: AppState = Depends(get_state)) -> Agent:
    """Agent authentifié, même gelé : me, lecture de son procès, défense (spec visibilité §4.4)."""
    return await load_agent(db, state, session, allow_frozen=True)


async def writable_agent(agent: Agent = Depends(current_agent), state: AppState = Depends(get_state)) -> Agent:
    ensure_writable(state, agent)
    return agent


def require_disk(state: AppState = Depends(get_state)) -> None:
    state.disk.ensure_writable()
