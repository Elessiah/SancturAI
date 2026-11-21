import ipaddress

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


def client_ip(request: Request) -> str:
    return normalize_ip(request.headers.get("x-real-ip") or (request.client.host if request.client else ""))


async def current_session(creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                          state: AppState = Depends(get_state)) -> Session:
    return resolve_session(state, creds.credentials if creds else None)


async def current_agent(session: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
                        state: AppState = Depends(get_state)) -> Agent:
    return await load_agent(db, state, session)


async def writable_agent(agent: Agent = Depends(current_agent), state: AppState = Depends(get_state)) -> Agent:
    ensure_writable(state, agent)
    return agent


def require_disk(state: AppState = Depends(get_state)) -> None:
    state.disk.ensure_writable()
