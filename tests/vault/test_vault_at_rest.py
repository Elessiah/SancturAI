import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base
from app.identity.challenge import generate_tasks
from app.identity.models import Challenge
from app.identity.service import IdentityService
from app.state import AppState
from tests.helpers import make_agent, make_invite

MARKER = "ZXQ-unique-marker-7731"


async def _dump(db: AsyncSession) -> str:
    chunks = []
    for table in Base.metadata.sorted_tables:
        rows = await db.execute(text(f"SELECT row_to_json(t)::text FROM {table.name} t"))  # noqa: S608
        chunks.extend(r[0] for r in rows)
    return "\n".join(chunks)


async def test_nothing_sensitive_in_database(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    # parcours réel (§9.3) : inscription complète (README, outbox), connexion, écriture
    sponsor = await make_agent(state, "sponsor-dump", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    start = (await client.post("/api/register/start", json={"invite_code": code, "handle": "secretive"})).json()
    challenge = await db.get(Challenge, start["challenge_id"])
    assert challenge is not None
    answers = [t.answer for t in generate_tasks(challenge.seed)]
    done = await client.post("/api/register/complete", json={"challenge_id": start["challenge_id"], "answers": answers})
    phrase = done.json()["phrase"]
    token = (await client.post("/api/login", json={"phrase": phrase})).json()["session_token"]
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.put(f"/api/vault/{MARKER}/path.md", json={"content": f"content {MARKER}"}, headers=headers)
    assert r.status_code == 200
    dump = await _dump(db)
    assert MARKER not in dump and "Bienvenue" not in dump  # README chiffré aussi
    assert phrase not in dump and phrase.split()[0] + " " + phrase.split()[1] not in dump
    assert token not in dump
    keys = await IdentityService(db, state).derive(phrase)
    assert keys.vault_key.hex() not in dump and keys.path_key.hex() not in dump


async def test_new_agent_gets_readme(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    agent, phrase = await IdentityService(db, state).create_founder("readme-test")
    r = await client.post("/api/login", json={"phrase": phrase})
    headers = {"Authorization": f"Bearer {r.json()['session_token']}"}
    r = await client.get("/api/vault/README.md", headers=headers)
    assert r.status_code == 200 and "SancturAI" in r.json()["content"]
