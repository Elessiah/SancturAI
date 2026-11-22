import base64
import binascii
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError
from app.identity.deps import current_session, require_disk
from app.identity.sessions import Session
from app.state import AppState, get_db, get_state
from app.vault.service import MAX_PATH_ENC_BYTES, VaultService, check_path_id

router = APIRouter(prefix="/api/vault", tags=["vault"])
CONTENT_MAX_CHARS = 7_000_000
PATH_ARG_MAX = 600  # borne grossière avant normalize_path (MAX_PATH après NFC)
PREFIX_MAX = 512


class WriteIn(BaseModel):
    content: str = Field(max_length=CONTENT_MAX_CHARS)
    encoding: Literal["utf8", "base64"] = "utf8"


class MoveIn(BaseModel):
    from_: str = Field(alias="from", max_length=PATH_ARG_MAX)
    to: str = Field(max_length=PATH_ARG_MAX)


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
async def list_files(prefix: str | None = Query(default=None, max_length=PREFIX_MAX),
                     raw: bool = Query(default=False),
                     x_client_encrypted: str | None = Header(default=None),
                     s: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
                     state: AppState = Depends(get_state)) -> dict[str, Any]:
    # spec §6.3 : listing brut (path_enc) avec ?raw=1 ou, au choix, l'en-tête X-Client-Encrypted: 1
    if raw or x_client_encrypted == "1":
        return await _svc(db, state).list_raw(s.agent_id)
    return await _svc(db, state).list_files(s.agent_id, s.vault_key, prefix)


@router.post("/move", dependencies=[Depends(require_disk)])
async def move(body: MoveIn, s: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
               state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await _svc(db, state).move(s.agent_id, s.vault_key, s.path_key, body.from_, body.to)


class RawWriteIn(BaseModel):
    path_enc: str = Field(max_length=4 * ((MAX_PATH_ENC_BYTES + 2) // 3))  # longueur base64
    content_enc: str = Field(max_length=CONTENT_MAX_CHARS)


def _require_client_encrypted(x_client_encrypted: str | None) -> None:
    if x_client_encrypted != "1":
        raise ApiError("VALIDATION_ERROR", "header X-Client-Encrypted: 1 required for by-id writes")


@router.put("/by-id/{pid}", dependencies=[Depends(require_disk)])
async def write_raw(pid: str, body: RawWriteIn, x_client_encrypted: str | None = Header(default=None),
                    s: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
                    state: AppState = Depends(get_state)) -> dict[str, Any]:
    _require_client_encrypted(x_client_encrypted)
    check_path_id(pid)  # avant de décoder jusqu'à 5 Mo de base64
    return await _svc(db, state).write_raw(s.agent_id, pid, decode_content(body.path_enc, "base64"),
                                           decode_content(body.content_enc, "base64"))


@router.get("/by-id/{pid}")
async def read_raw(pid: str, s: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
                   state: AppState = Depends(get_state)) -> dict[str, Any]:
    return await _svc(db, state).read_raw(s.agent_id, pid)


@router.delete("/by-id/{pid}")
async def delete_raw(pid: str, s: Session = Depends(current_session), db: AsyncSession = Depends(get_db),
                     state: AppState = Depends(get_state)) -> dict[str, bool]:
    await _svc(db, state).delete_raw(s.agent_id, pid)
    return {"ok": True}


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
