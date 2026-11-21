import base64
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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

    async def _get(self, agent_id: str, pid: str, *, lock: bool = False) -> VaultFile | None:
        return await self._db.get(VaultFile, (agent_id, pid), with_for_update=lock)

    async def put_raw(self, agent_id: str, pid: str, path_enc: bytes, content_enc: bytes, size: int) -> VaultFile:
        """Écriture commune (serveur et E2E). Vérifie les quotas, ne commit pas."""
        if size > self._settings.vault_file_max_bytes:
            raise ApiError("QUOTA_EXCEEDED", f"file larger than {self._settings.vault_file_max_bytes} bytes")
        await self._lock_agent(agent_id)
        existing = await self._get(agent_id, pid, lock=True)
        projected = await self.usage(agent_id) - (existing.size if existing else 0) + size
        if projected > self._settings.vault_quota_bytes:
            raise ApiError("QUOTA_EXCEEDED", f"vault quota {self._settings.vault_quota_bytes} bytes")
        if existing is None:
            existing = VaultFile(agent_id=agent_id, path_id=pid, path_enc=path_enc, content_enc=content_enc,
                                 size=size)
            self._db.add(existing)
        else:
            existing.path_enc, existing.content_enc, existing.size = path_enc, content_enc, size
        await self._db.flush()
        await self._db.refresh(existing)
        return existing

    async def _put(self, agent_id: str, vault_key: bytes, path_key: bytes, path: str, data: bytes) -> VaultFile:
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

    async def _require(self, agent_id: str, path_key: bytes, path: str, *, lock: bool = False
                       ) -> tuple[str, VaultFile]:
        path = normalize_path(path)
        row = await self._get(agent_id, path_id(path_key, path), lock=lock)
        if row is None:
            raise ApiError("NOT_FOUND", f"no file {path}")
        return path, row

    async def read(self, agent_id: str, vault_key: bytes, path_key: bytes, path: str) -> dict[str, Any]:
        path, row = await self._require(agent_id, path_key, path)
        try:
            data = decrypt(vault_key, row.content_enc, aad_content(agent_id, row.path_id))
        except CryptoError as exc:
            raise ApiError("FORBIDDEN", "file was encrypted client-side; read it by path_id") from exc
        content, encoding = _encode(data)
        return {**file_meta(path, row), "content": content, "encoding": encoding}

    async def list_files(self, agent_id: str, vault_key: bytes, prefix: str | None) -> dict[str, Any]:
        rows = await self._db.execute(
            select(VaultFile.path_id, VaultFile.path_enc, VaultFile.size, VaultFile.updated_at)
            .where(VaultFile.agent_id == agent_id))
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

    async def delete(self, agent_id: str, path_key: bytes, path: str) -> None:
        _, row = await self._require(agent_id, path_key, path)
        await self._db.delete(row)
        await self._db.commit()

    async def move(self, agent_id: str, vault_key: bytes, path_key: bytes, src: str, dst: str) -> dict[str, Any]:
        src, row = await self._require(agent_id, path_key, src, lock=True)
        dst = normalize_path(dst)
        if await self._get(agent_id, path_id(path_key, dst)) is not None:
            raise ApiError("VALIDATION_ERROR", f"{dst} already exists")
        try:
            data = decrypt(vault_key, row.content_enc, aad_content(agent_id, row.path_id))
        except CryptoError as exc:
            raise ApiError("FORBIDDEN", "file was encrypted client-side; move it by path_id") from exc
        await self._db.delete(row)
        await self._db.flush()
        new = await self._put(agent_id, vault_key, path_key, dst, data)
        await self._db.commit()
        return file_meta(dst, new)
