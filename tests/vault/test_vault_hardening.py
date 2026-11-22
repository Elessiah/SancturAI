import unicodedata
from datetime import UTC, datetime

import httpx
from sqlalchemy import delete, event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models import Agent, Invite
from app.identity.service import IdentityService
from app.state import AppState
from app.vault.service import VaultService
from tests.helpers import make_agent


async def test_raw_listing_header_alias(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "alias")
    await client.put("/api/vault/a.md", json={"content": "x"}, headers=a.headers)
    by_query = (await client.get("/api/vault", params={"raw": 1}, headers=a.headers)).json()
    by_header = (await client.get("/api/vault", headers={**a.headers, "X-Client-Encrypted": "1"})).json()
    assert by_header == by_query and "path_enc" in by_header["files"][0]


async def test_agent_row_lock_does_not_block_fk_inserts(state: AppState) -> None:
    a = await make_agent(state, "locker", founder=True)
    async with state.sessionmaker() as holder, state.sessionmaker() as other:
        await VaultService(holder, state.settings)._lock_agent(a.agent_id)  # transaction ouverte, verrou tenu
        await other.execute(text("SET LOCAL lock_timeout = '1s'"))
        other.add(Invite(code="fk-insert-ok", creator_id=a.agent_id, expires_at=datetime.now(UTC)))
        await other.commit()  # FOR UPDATE bloquerait ici (FOR KEY SHARE de la FK)
        await holder.rollback()


async def test_create_invite_lock_is_no_key_update(state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "inviter-lock", founder=True)
    agent = await db.get(Agent, a.agent_id)
    assert agent is not None
    statements: list[str] = []
    def capture(conn: object, cursor: object, stmt: str, *args: object) -> None:
        statements.append(stmt)

    event.listen(state.engine.sync_engine, "before_cursor_execute", capture)
    try:
        await IdentityService(db, state).create_invite(agent)
    finally:
        event.remove(state.engine.sync_engine, "before_cursor_execute", capture)
    locks = [s for s in statements if "FROM agents" in s and " FOR " in s]
    assert locks and all("FOR NO KEY UPDATE" in s for s in locks)


async def test_list_prefix_is_nfc_normalized(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "nfc")
    await client.put("/api/vault/café/a.md", json={"content": "x"}, headers=a.headers)
    nfd = unicodedata.normalize("NFD", "café")
    for prefix in (f"/{nfd}", nfd):
        files = (await client.get("/api/vault", params={"prefix": prefix}, headers=a.headers)).json()["files"]
        assert [f["path"] for f in files] == ["/café/a.md"], prefix


async def test_write_after_agent_deleted_is_unauthenticated(client: httpx.AsyncClient, state: AppState,
                                                            db: AsyncSession) -> None:
    a = await make_agent(state, "ghost")
    await db.execute(delete(Agent).where(Agent.agent_id == a.agent_id))
    await db.commit()
    r = await client.put("/api/vault/a.md", json={"content": "x"}, headers=a.headers)
    assert (r.status_code, r.json()["error"]) == (401, "UNAUTHENTICATED")
    assert state.errors.take() == 0


async def test_suspended_agent_keeps_vault_access(client: httpx.AsyncClient, state: AppState,
                                                  db: AsyncSession) -> None:
    a = await make_agent(state, "suspended-vault", age_days=10)
    agent = await db.get(Agent, a.agent_id)
    assert agent is not None
    await IdentityService(db, state).suspend(agent, days=7, reason="test")
    await db.commit()
    assert (await client.put("/api/vault/s.md", json={"content": "x"}, headers=a.headers)).status_code == 200
    assert (await client.get("/api/vault/s.md", headers=a.headers)).json()["content"] == "x"
    r = await client.post("/api/vault/move", json={"from": "/s.md", "to": "/t.md"}, headers=a.headers)
    assert r.status_code == 200, r.text
    assert (await client.delete("/api/vault/t.md", headers=a.headers)).status_code == 200


async def test_undecryptable_file_message_is_neutral(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "neutral")
    await client.put("/api/vault/z.md", json={"content": "x"}, headers=a.headers)
    pid = (await client.get("/api/vault", params={"raw": 1}, headers=a.headers)).json()["files"][0]["path_id"]
    await client.put(f"/api/vault/by-id/{pid}", json={"path_enc": "AA==", "content_enc": "AA=="},
                     headers={**a.headers, "X-Client-Encrypted": "1"})
    r = (await client.get("/api/vault/z.md", headers=a.headers)).json()
    assert r["error"] == "FORBIDDEN"
    assert "cannot be decrypted with server-side keys" in r["message"] and "by path_id" in r["message"]
