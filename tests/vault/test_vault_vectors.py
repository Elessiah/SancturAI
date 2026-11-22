"""Le format v1 des AAD du vault est figé par tests/vectors/crypto_v1.json, pas par le module testé."""
import json
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app import crypto
from app.identity.models import Agent
from app.state import AppState
from app.vault.models import VaultFile

VEC = json.loads((Path(__file__).parents[1] / "vectors" / "crypto_v1.json").read_text())


def test_vault_aad_bytes_are_frozen() -> None:
    pid = VEC["path_id_readme"]
    assert crypto.aad_path(VEC["agent_id"], pid) == VEC["aad_path_readme"].encode()
    assert crypto.aad_content(VEC["agent_id"], pid) == VEC["aad_content_readme"].encode()


def test_frozen_vault_blobs_decrypt_with_frozen_aad() -> None:
    key = bytes.fromhex(VEC["vault_key_hex"])
    path = crypto.decrypt(key, bytes.fromhex(VEC["readme_path_enc_hex"]), VEC["aad_path_readme"].encode())
    content = crypto.decrypt(key, bytes.fromhex(VEC["readme_content_enc_hex"]), VEC["aad_content_readme"].encode())
    assert path == b"/README.md"
    assert content.decode() == VEC["readme_content"]


async def test_server_reads_frozen_vault_blobs(client: httpx.AsyncClient, state: AppState,
                                               db: AsyncSession) -> None:
    db.add(Agent(agent_id=VEC["agent_id"], handle="vector"))
    await db.flush()
    db.add(VaultFile(agent_id=VEC["agent_id"], path_id=VEC["path_id_readme"],
                     path_enc=bytes.fromhex(VEC["readme_path_enc_hex"]),
                     content_enc=bytes.fromhex(VEC["readme_content_enc_hex"]), size=len(VEC["readme_content"])))
    await db.commit()
    token, _ = state.sessions.create(VEC["agent_id"], bytes.fromhex(VEC["vault_key_hex"]),
                                     bytes.fromhex(VEC["path_key_hex"]))
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/vault/README.md", headers=headers)).json()["content"] == VEC["readme_content"]
    files = (await client.get("/api/vault", headers=headers)).json()["files"]
    assert [f["path"] for f in files] == ["/README.md"]
