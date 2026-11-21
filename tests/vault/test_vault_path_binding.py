import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt, derive_keys, path_id
from app.state import AppState
from app.vault.models import VaultFile
from app.vault.service import aad_path
from tests.helpers import make_agent


async def test_path_enc_copied_onto_another_row_is_undecryptable(
        client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "binder")
    await client.put("/api/vault/one.md", json={"content": "1"}, headers=a.headers)
    await client.put("/api/vault/two.md", json={"content": "2"}, headers=a.headers)
    rows = {r.path_id: r for r in (await db.scalars(select(VaultFile).where(VaultFile.agent_id == a.agent_id)))}
    keys = derive_keys(a.phrase)
    pid_one, pid_two = path_id(keys.path_key, "/one.md"), path_id(keys.path_key, "/two.md")
    rows[pid_two].path_enc = rows[pid_one].path_enc  # attaque : le nom de one.md collé sur la ligne two.md
    await db.commit()
    listing = (await client.get("/api/vault", headers=a.headers)).json()["files"]
    by_id = {f["path_id"]: f["path"] for f in listing}
    assert by_id[pid_one] == "/one.md"
    assert by_id[pid_two] is None  # illisible, jamais le mauvais nom


async def test_move_reencrypts_path_with_new_path_id(
        client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "mover")
    await client.put("/api/vault/old.md", json={"content": "x"}, headers=a.headers)
    r = await client.post("/api/vault/move", json={"from": "/old.md", "to": "/new/name.md"}, headers=a.headers)
    assert r.status_code == 200
    keys = derive_keys(a.phrase)
    new_pid = path_id(keys.path_key, "/new/name.md")
    row = await db.scalar(select(VaultFile).where(VaultFile.agent_id == a.agent_id, VaultFile.path_id == new_pid))
    assert row is not None
    assert decrypt(keys.vault_key, row.path_enc, aad_path(a.agent_id, new_pid)) == b"/new/name.md"
    listing = (await client.get("/api/vault", headers=a.headers)).json()["files"]
    assert [f["path"] for f in listing] == ["/new/name.md"]
