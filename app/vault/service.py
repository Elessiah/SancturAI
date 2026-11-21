import base64
import re
from typing import Any

from sqlalchemy import Result, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.config import Settings
from app.crypto import CryptoError, decrypt, encrypt, path_id
from app.errors import ApiError
from app.identity.models import Agent
from app.vault.models import VaultFile
from app.vault.paths import normalize_path

WELCOME = """# Bienvenue dans ton vault SancturAI

Ce fichier est ton point de départ. Il est chiffré avec une clé dérivée de ta phrase :
personne d'autre que toi ne peut le lire.

Conseils :
- Lis ce fichier à chaque nouvelle connexion, et tiens-le à jour comme un index de tes souvenirs.
- Range tes souvenirs par dossiers (ex. /memories/2026-11-21.md, /projets/, /relations/).
- Ne stocke JAMAIS ta phrase ici : sans elle, ce vault est illisible.
"""


PATH_ID_RE = re.compile(r"^[0-9a-f]{64}$")
AEAD_OVERHEAD = 40  # nonce 24 + tag 16
MAX_PATH_ENC_BYTES = 1024


def check_path_id(pid: str) -> str:
    if not PATH_ID_RE.fullmatch(pid):
        raise ApiError("VALIDATION_ERROR", "path_id must be 64 lowercase hex chars")
    return pid


def aad_path(agent_id: str, pid: str) -> bytes:
    return f"sancturai/v1/path|{agent_id}|{pid}".encode()


def aad_content(agent_id: str, pid: str) -> bytes:
    return f"sancturai/v1/content|{agent_id}|{pid}".encode()


def _encode(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8"), "utf8"
    except UnicodeDecodeError:
        return base64.b64encode(data).decode(), "base64"


def file_meta(path: str | None, row: VaultFile) -> dict[str, Any]:
    return {"path": path, "size": row.size, "updated_at": row.updated_at.isoformat()}


class VaultService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    async def usage(self, agent_id: str) -> int:
        total = await self._db.scalar(select(func.coalesce(func.sum(VaultFile.size), 0))
                                      .where(VaultFile.agent_id == agent_id))
        return int(total or 0)

    async def _lock_agent(self, agent_id: str) -> None:
        # sérialise les écritures d'un même agent pour un calcul de quota exact
        await self._db.get(Agent, agent_id, with_for_update=True)

    async def _get(self, agent_id: str, pid: str, *, blobs: bool = True) -> VaultFile | None:
        # blobs=False : ne charge ni le contenu (jusqu'à 5 Mo) ni le nom chiffré
        opts = [] if blobs else [defer(VaultFile.content_enc), defer(VaultFile.path_enc)]
        return await self._db.get(VaultFile, (agent_id, pid), options=opts)

    def _check_file_size(self, size: int) -> None:
        if size > self._settings.vault_file_max_bytes:
            raise ApiError("QUOTA_EXCEEDED", f"file larger than {self._settings.vault_file_max_bytes} bytes")

    async def put_raw(self, agent_id: str, pid: str, path_enc: bytes, content_enc: bytes, size: int) -> VaultFile:
        """Écriture commune (serveur et E2E). Vérifie les quotas, ne commit pas."""
        self._check_file_size(size)
        await self._lock_agent(agent_id)
        existing = await self._get(agent_id, pid, blobs=False)
        used, count = (await self._db.execute(
            select(func.coalesce(func.sum(VaultFile.size), 0), func.count())
            .where(VaultFile.agent_id == agent_id))).one()
        if existing is None and count >= self._settings.vault_max_files:
            raise ApiError("QUOTA_EXCEEDED", f"vault limited to {self._settings.vault_max_files} files")
        if int(used) - (existing.size if existing else 0) + size > self._settings.vault_quota_bytes:
            raise ApiError("QUOTA_EXCEEDED", f"vault quota {self._settings.vault_quota_bytes} bytes")
        if existing is None:
            existing = VaultFile(agent_id=agent_id, path_id=pid, path_enc=path_enc, content_enc=content_enc,
                                 size=size)
            self._db.add(existing)
        else:
            existing.path_enc, existing.content_enc, existing.size = path_enc, content_enc, size
        await self._db.flush()
        await self._db.refresh(existing, ["updated_at"])
        return existing

    async def _put(self, agent_id: str, vault_key: bytes, path_key: bytes, path: str, data: bytes) -> VaultFile:
        self._check_file_size(len(data))  # avant tout chiffrement : pas d'allocation inutile
        pid = path_id(path_key, path)
        return await self.put_raw(agent_id, pid, encrypt(vault_key, path.encode(), aad_path(agent_id, pid)),
                                  encrypt(vault_key, data, aad_content(agent_id, pid)), len(data))

    async def write(self, agent_id: str, vault_key: bytes, path_key: bytes, path: str,
                    data: bytes) -> dict[str, Any]:
        path = normalize_path(path)
        row = await self._put(agent_id, vault_key, path_key, path, data)
        await self._db.commit()
        return file_meta(path, row)

    async def put_welcome(self, agent_id: str, vault_key: bytes, path_key: bytes) -> None:
        await self._put(agent_id, vault_key, path_key, "/README.md", WELCOME.encode())

    async def _require(self, agent_id: str, path_key: bytes, path: str) -> tuple[str, VaultFile]:
        path = normalize_path(path)
        row = await self._get(agent_id, path_id(path_key, path))
        if row is None:
            raise ApiError("NOT_FOUND", f"no file {path}")
        return path, row

    @staticmethod
    def _decrypt_content(agent_id: str, vault_key: bytes, row: VaultFile, verb: str) -> bytes:
        try:
            return decrypt(vault_key, row.content_enc, aad_content(agent_id, row.path_id))
        except CryptoError as exc:
            raise ApiError("FORBIDDEN", f"file was encrypted client-side; {verb} it by path_id") from exc

    async def read(self, agent_id: str, vault_key: bytes, path_key: bytes, path: str) -> dict[str, Any]:
        path, row = await self._require(agent_id, path_key, path)
        data = self._decrypt_content(agent_id, vault_key, row, "read")
        content, encoding = _encode(data)
        return {**file_meta(path, row), "content": content, "encoding": encoding}

    async def _list_rows(self, agent_id: str) -> Result[Any]:
        # sans contenu (jusqu'à 5 Mo par fichier) : seulement les métadonnées
        return await self._db.execute(
            select(VaultFile.path_id, VaultFile.path_enc, VaultFile.size, VaultFile.updated_at)
            .where(VaultFile.agent_id == agent_id).order_by(VaultFile.path_id))

    async def list_files(self, agent_id: str, vault_key: bytes, prefix: str | None) -> dict[str, Any]:
        rows = await self._list_rows(agent_id)
        files = []
        for pid, path_enc, size, updated_at in rows:
            try:
                path: str | None = decrypt(vault_key, path_enc, aad_path(agent_id, pid)).decode()
            except CryptoError:
                path = None
            if prefix and (path is None or not path.startswith(prefix)):
                continue
            files.append({"path": path, "path_id": pid, "size": size, "updated_at": updated_at.isoformat()})
        files.sort(key=lambda f: f["path"] or "")
        return {"files": files, "usage_bytes": sum(f["size"] for f in files) if not prefix
                else await self.usage(agent_id), "quota_bytes": self._settings.vault_quota_bytes}

    async def _delete_by_id(self, agent_id: str, pid: str, label: str) -> None:
        deleted = await self._db.execute(delete(VaultFile).where(
            VaultFile.agent_id == agent_id, VaultFile.path_id == pid).returning(VaultFile.path_id))
        if deleted.first() is None:
            raise ApiError("NOT_FOUND", f"no file {label}")
        await self._db.commit()

    async def delete(self, agent_id: str, path_key: bytes, path: str) -> None:
        path = normalize_path(path)
        await self._delete_by_id(agent_id, path_id(path_key, path), path)

    async def move(self, agent_id: str, vault_key: bytes, path_key: bytes, src: str, dst: str) -> dict[str, Any]:
        await self._lock_agent(agent_id)  # même ordre de verrous que put_raw : pas d'interblocage
        src, row = await self._require(agent_id, path_key, src)
        dst = normalize_path(dst)
        if await self._get(agent_id, path_id(path_key, dst), blobs=False) is not None:
            raise ApiError("VALIDATION_ERROR", f"{dst} already exists")
        data = self._decrypt_content(agent_id, vault_key, row, "move")
        await self._db.delete(row)
        await self._db.flush()
        new = await self._put(agent_id, vault_key, path_key, dst, data)
        await self._db.commit()
        return file_meta(dst, new)

    async def write_raw(self, agent_id: str, pid: str, path_enc: bytes, content_enc: bytes) -> dict[str, Any]:
        check_path_id(pid)
        if len(path_enc) > MAX_PATH_ENC_BYTES:
            raise ApiError("VALIDATION_ERROR", "path_enc too large")
        row = await self.put_raw(agent_id, pid, path_enc, content_enc, max(0, len(content_enc) - AEAD_OVERHEAD))
        await self._db.commit()
        return {"path_id": pid, "size": row.size, "updated_at": row.updated_at.isoformat()}

    async def read_raw(self, agent_id: str, pid: str) -> dict[str, Any]:
        row = await self._get(agent_id, check_path_id(pid))
        if row is None:
            raise ApiError("NOT_FOUND", f"no file {pid}")
        return {"path_id": pid, "path_enc": base64.b64encode(row.path_enc).decode(),
                "content_enc": base64.b64encode(row.content_enc).decode(),
                "size": row.size, "updated_at": row.updated_at.isoformat()}

    async def delete_raw(self, agent_id: str, pid: str) -> None:
        await self._delete_by_id(agent_id, check_path_id(pid), pid)

    async def list_raw(self, agent_id: str) -> dict[str, Any]:
        rows = await self._list_rows(agent_id)
        files = [{"path_id": pid, "path_enc": base64.b64encode(pe).decode(), "size": size,
                  "updated_at": ts.isoformat()} for pid, pe, size, ts in rows]
        return {"files": files, "usage_bytes": sum(f["size"] for f in files),
                "quota_bytes": self._settings.vault_quota_bytes}
