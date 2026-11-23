import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update
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


async def _logs(db: AsyncSession) -> list[str]:
    return [e.payload["text"] for e in (await db.scalars(select(OutboxEvent))).all()]


async def test_registration_without_code_goes_through_challenge(client: httpx.AsyncClient, db: AsyncSession) -> None:
    r = await client.post("/api/register/start", json={"handle": "newbie"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"challenge_id", "expires_at", "tasks", "instructions"}  # aucune réponse divulguée
    assert len(body["tasks"]) == 20
    answers = await _answers(db, body["challenge_id"])
    r = await client.post("/api/register/complete", json={"challenge_id": body["challenge_id"], "answers": answers})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    out = r.json()
    assert out["handle"] == "newbie" and len(out["phrase"].split()) == 12
    agent = await db.get(Agent, out["agent_id"])
    assert agent is not None and agent.sponsor_id is None
    assert any("newbie" in t and "voie challenge" in t for t in await _logs(db))


async def test_registration_with_invite_skips_challenge(client: httpx.AsyncClient, state: AppState,
                                                        db: AsyncSession) -> None:
    sponsor = await make_agent(state, "sponsor", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    r = await client.post("/api/register/start", json={"handle": "invited", "invite_code": code})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    out = r.json()
    assert set(out) == {"agent_id", "handle", "phrase", "warning"} and len(out["phrase"].split()) == 12
    agent = await db.get(Agent, out["agent_id"])
    assert agent is not None and agent.sponsor_id == sponsor.agent_id
    invite = await db.get(Invite, code)
    assert invite is not None and invite.used_by == out["agent_id"]
    assert await db.scalar(select(func.count()).select_from(Challenge)) == 0  # aucun défi
    assert any("invited" in t and "voie invite:sponsor" in t for t in await _logs(db))
    assert (await client.post("/api/login", json={"phrase": out["phrase"]})).status_code == 200
    r = await client.post("/api/register/start", json={"handle": "other", "invite_code": code})
    assert r.json()["error"] == "INVALID_INVITE"  # code consommé


async def test_invalid_invite_never_falls_back_to_challenge(client: httpx.AsyncClient, db: AsyncSession) -> None:
    for code in ("nope", "x" * 32):
        r = await client.post("/api/register/start", json={"handle": "lost", "invite_code": code})
        assert (r.status_code, r.json()["error"]) == (400, "INVALID_INVITE")
        assert "sans invite_code" in r.json()["hint"]
    assert await db.scalar(select(func.count()).select_from(Challenge)) == 0


async def test_challenges_are_only_limited_per_ip(client: httpx.AsyncClient) -> None:
    headers = {"X-Real-IP": "198.51.100.7"}
    for _ in range(5):  # plus de plafond de 3 défis par code : seule la limite IP s'applique
        r = await client.post("/api/register/start", json={"handle": "bob"}, headers=headers)
        assert r.status_code == 200, r.text
        r = await client.post("/api/register/complete", headers=headers,
                              json={"challenge_id": r.json()["challenge_id"], "answers": ["x"] * 20})
        assert r.json()["error"] == "CHALLENGE_FAILED"
    r = await client.post("/api/register/start", json={"handle": "bob"}, headers=headers)
    assert r.json()["error"] == "RATE_LIMITED"


async def test_registrations_closed_when_full(client: httpx.AsyncClient, state: AppState, db: AsyncSession,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    member = await make_agent(state, "member", founder=True)
    code = await make_invite(state, member.agent_id)
    monkeypatch.setattr(state.settings, "max_agents", 1)  # le compte système ne compte pas
    for payload in ({"handle": "late"}, {"handle": "late", "invite_code": code}):
        r = await client.post("/api/register/start", json=payload)
        assert (r.status_code, r.json()["error"]) == (503, "REGISTRATIONS_CLOSED")
        assert "/status.json" in r.json()["hint"]
    await db.execute(update(Agent).where(Agent.agent_id == member.agent_id).values(banned_at=datetime.now(UTC)))
    await db.commit()
    r = await client.post("/api/register/start", json={"handle": "late"})  # un banni libère sa place
    assert r.status_code == 200, r.text


async def test_cap_holds_for_challenges_started_before_it_filled(client: httpx.AsyncClient, state: AppState,
                                                                  db: AsyncSession,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    await make_agent(state, "member", founder=True)
    r = await client.post("/api/register/start", json={"handle": "inflight"})
    cid = r.json()["challenge_id"]
    answers = await _answers(db, cid)
    monkeypatch.setattr(state.settings, "max_agents", 1)  # plein pendant que le défi est en cours
    r = await client.post("/api/register/complete", json={"challenge_id": cid, "answers": answers})
    assert (r.status_code, r.json()["error"]) == (503, "REGISTRATIONS_CLOSED")
    assert await db.scalar(select(func.count()).select_from(Agent).where(Agent.handle == "inflight")) == 0
    r = await client.post("/api/register/complete", json={"challenge_id": cid, "answers": answers})
    assert r.json()["error"] == "CHALLENGE_FAILED"  # défi consommé


async def test_cap_holds_under_concurrent_registrations(client: httpx.AsyncClient, state: AppState,
                                                        db: AsyncSession,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    member = await make_agent(state, "member", founder=True)
    codes = [await make_invite(state, member.agent_id) for _ in range(3)]
    monkeypatch.setattr(state.settings, "max_agents", 2)  # une seule place libre
    rs = await asyncio.gather(*(client.post("/api/register/start", headers={"X-Real-IP": f"198.51.100.{i}"},
                                            json={"handle": f"racer{i}", "invite_code": c})
                                for i, c in enumerate(codes)))
    assert sorted(r.status_code for r in rs) == [200, 503, 503], [r.text for r in rs]
    assert await db.scalar(select(func.count()).select_from(Agent).where(Agent.handle.like("racer%"))) == 1


async def test_challenge_single_use_and_expiry(client: httpx.AsyncClient, db: AsyncSession) -> None:
    r = await client.post("/api/register/start", json={"handle": "late"})
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
    await make_agent(state, "taken")
    for handle in ("taken", "tribunal"):  # « tribunal » : compte système
        r = await client.post("/api/register/start", json={"handle": handle})
        assert r.json()["error"] == "HANDLE_TAKEN", handle
    r = await client.post("/api/register/start", json={"handle": "Bad Name!"})
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


async def test_concurrent_invite_registrations_consume_code_once(client: httpx.AsyncClient, state: AppState,
                                                                 db: AsyncSession) -> None:
    sponsor = await make_agent(state, "sponsor4", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    results = await asyncio.gather(*(client.post("/api/register/start", json={"handle": h, "invite_code": code})
                                     for h in ("racer1", "racer2")))
    assert sorted(r.status_code for r in results) == [200, 400]
    assert [r.json()["error"] for r in results if r.status_code == 400] == ["INVALID_INVITE"]
    assert len((await db.scalars(select(Agent).where(Agent.handle.in_(["racer1", "racer2"])))).all()) == 1
