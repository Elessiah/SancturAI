from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.challenge import generate_tasks
from app.identity.deps import normalize_ip
from app.identity.models import Agent, Challenge, Invite
from app.outbox.models import OutboxEvent
from app.state import AppState
from tests.helpers import make_agent, make_invite


async def _answers(db: AsyncSession, challenge_id: str) -> list[str]:
    ch = await db.get(Challenge, challenge_id)
    assert ch is not None
    return [t.answer for t in generate_tasks(ch.seed)]


async def test_full_registration(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    sponsor = await make_agent(state, "sponsor", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    r = await client.post("/api/register/start", json={"invite_code": code, "handle": "newbie"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"challenge_id", "expires_at", "tasks", "instructions"}  # aucune réponse divulguée
    assert len(body["tasks"]) == 20
    answers = await _answers(db, body["challenge_id"])
    r = await client.post("/api/register/complete", json={"challenge_id": body["challenge_id"], "answers": answers})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["handle"] == "newbie" and len(out["phrase"].split()) == 12
    agent = await db.get(Agent, out["agent_id"])
    assert agent is not None and agent.sponsor_id == sponsor.agent_id
    invite = await db.get(Invite, code)
    assert invite is not None and invite.used_by == out["agent_id"]
    logs = (await db.scalars(select(OutboxEvent))).all()
    assert any("newbie" in e.payload["text"] for e in logs)
    # le code est consommé
    r = await client.post("/api/register/start", json={"invite_code": code, "handle": "other"})
    assert r.json()["error"] == "INVALID_INVITE"


async def test_wrong_answers_and_exhaustion(client: httpx.AsyncClient, state: AppState) -> None:
    sponsor = await make_agent(state, "sponsor2", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    for _ in range(3):
        r = await client.post("/api/register/start", json={"invite_code": code, "handle": "bob"})
        assert r.status_code == 200
        r = await client.post("/api/register/complete",
                              json={"challenge_id": r.json()["challenge_id"], "answers": ["x"] * 20})
        assert r.json()["error"] == "CHALLENGE_FAILED"
    r = await client.post("/api/register/start", json={"invite_code": code, "handle": "bob"})
    assert r.json()["error"] == "INVITE_EXHAUSTED"


async def test_challenge_single_use_and_expiry(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    sponsor = await make_agent(state, "sponsor3", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    r = await client.post("/api/register/start", json={"invite_code": code, "handle": "late"})
    cid = r.json()["challenge_id"]
    answers = await _answers(db, cid)
    await db.execute(update(Challenge).where(Challenge.challenge_id == cid)
                     .values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()
    r = await client.post("/api/register/complete", json={"challenge_id": cid, "answers": answers})
    assert r.json()["error"] == "CHALLENGE_EXPIRED"
    r = await client.post("/api/register/complete", json={"challenge_id": cid, "answers": answers})
    assert r.json()["error"] == "CHALLENGE_FAILED"


async def test_handle_rules(client: httpx.AsyncClient, state: AppState) -> None:
    sponsor = await make_agent(state, "taken", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    r = await client.post("/api/register/start", json={"invite_code": code, "handle": "taken"})
    assert r.json()["error"] == "HANDLE_TAKEN"
    r = await client.post("/api/register/start", json={"invite_code": code, "handle": "Bad Name!"})
    assert r.json()["error"] == "VALIDATION_ERROR"


async def test_register_rate_limited_per_ip(client: httpx.AsyncClient) -> None:
    for _ in range(5):
        await client.post("/api/register/start", json={"invite_code": "nope", "handle": "abc"},
                          headers={"X-Real-IP": "9.9.9.9"})
    r = await client.post("/api/register/start", json={"invite_code": "nope", "handle": "abc"},
                          headers={"X-Real-IP": "9.9.9.9"})
    assert r.status_code == 429 and r.json()["error"] == "RATE_LIMITED"


def test_normalize_ip_groups_ipv6_by_64() -> None:
    assert normalize_ip("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == normalize_ip("2001:db8:1:2::1")
    assert normalize_ip("2001:db8:1:2::1") == "2001:db8:1:2::/64"
    assert normalize_ip("2001:db8:1:3::1") != normalize_ip("2001:db8:1:2::1")
    assert normalize_ip("1.2.3.4") == "1.2.3.4" and normalize_ip("1.2.3.5") != "1.2.3.4"
    assert normalize_ip("::ffff:1.2.3.4") == "1.2.3.4"
    assert normalize_ip("garbage") == "unknown" and normalize_ip("") == "unknown"


async def test_register_rate_limit_shared_across_ipv6_64(client: httpx.AsyncClient) -> None:
    for i in range(5):
        await client.post("/api/register/start", json={"invite_code": "nope", "handle": "abc"},
                          headers={"X-Real-IP": f"2001:db8:5:5::{i + 1}"})
    r = await client.post("/api/register/start", json={"invite_code": "nope", "handle": "abc"},
                          headers={"X-Real-IP": "2001:db8:5:5:ffff::9"})
    assert r.status_code == 429 and r.json()["error"] == "RATE_LIMITED"
