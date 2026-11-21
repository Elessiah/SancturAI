import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.identity.service import IdentityService
from app.state import AppState
from tests.helpers import make_agent

MARKER = "ZXQ-unique-marker-7731"


async def _dump(db: AsyncSession) -> str:
    chunks = []
    for table in Base.metadata.sorted_tables:
        rows = await db.execute(text(f"SELECT row_to_json(t)::text FROM {table.name} t"))  # noqa: S608
        chunks.extend(r[0] for r in rows)
    return "\n".join(chunks)


async def test_nothing_sensitive_in_database(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "secretive")
    await client.put(f"/api/vault/{MARKER}/path.md", json={"content": f"content {MARKER}"}, headers=a.headers)
    dump = await _dump(db)
    assert MARKER not in dump
    assert a.phrase not in dump and a.phrase.split()[0] + " " + a.phrase.split()[1] not in dump
    keys = await IdentityService(db, state).derive(a.phrase)
    assert keys.vault_key.hex() not in dump and keys.path_key.hex() not in dump


async def test_new_agent_gets_readme(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    agent, phrase = await IdentityService(db, state).create_founder("readme-test")
    r = await client.post("/api/login", json={"phrase": phrase})
    headers = {"Authorization": f"Bearer {r.json()['session_token']}"}
    r = await client.get("/api/vault/README.md", headers=headers)
    assert r.status_code == 200 and "SancturAI" in r.json()["content"]
