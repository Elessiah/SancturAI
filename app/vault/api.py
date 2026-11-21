import base64
import binascii
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.identity.deps import current_session, require_disk
from app.identity.sessions import Session
from app.state import AppState, get_db, get_state
from app.vault.service import VaultService

router = APIRouter(prefix="/api/vault", tags=["vault"])


class WriteIn(BaseModel):
    content: str = Field(max_length=7_000_000)
    encoding: Literal["utf8", "base64"] = "utf8"


class MoveIn(BaseModel):
    from_: str = Field(alias="from", max_length=600)
    to: str = Field(max_length=600)


def decode_content(content: str, encoding: str) -> bytes:
    if encoding == "utf8":
        return content.encode("utf-8")
    try:
        return base64.b64decode(content, validate=True)
    except binascii.Error as exc:
        raise ApiError("VALIDATION_ERROR", "invalid base64 content") from exc


def _svc(db: AsyncSession, state: AppState) -> VaultService:
    return VaultService(db, state.settings)


@router.get("")
async def list_files(prefix: str | None = Query(default=None, max_length=512),
                     s: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
                     state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await _svc(db, state).list_files(s.agent_id, s.vault_key, prefix)


@router.post("/move", dependencies=[Depends(require_disk)])
async def move(body: MoveIn, s: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
               state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await _svc(db, state).move(s.agent_id, s.vault_key, s.path_key, body.from_, body.to)


@router.put("/{path:path}", dependencies=[Depends(require_disk)])
async def write(path: str, body: WriteIn, s: Session = Depends(current_session),
                db: AsyncSession = Depends(get_db), state: AppState = Depends(get_state)) -> dict[str, Any]:
    data = decode_content(body.content, body.encoding)
    return await _svc(db, state).write(s.agent_id, s.vault_key, s.path_key, path, data)


@router.get("/{path:path}")
async def read(path: str, s: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
               state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await _svc(db, state).read(s.agent_id, s.vault_key, s.path_key, path)


@router.delete("/{path:path}")
async def delete(path: str, s: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
                 state: AppState = Depends(get_state)) -> dict[str, bool]:
    await _svc(db, state).delete(s.agent_id, s.path_key, path)
    return {"ok": True}
