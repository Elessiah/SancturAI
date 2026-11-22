import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin_messages.models import AdminMessage
from app.identity.models import Agent
from app.identity.service import IdentityService
from app.outbox.models import OutboxEvent
from app.state import AppState
from tests.helpers import make_agent

INTERNAL = {"x-internal-token": "test-internal-token"}


async def test_message_reply_inbox(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "talker")
    r = await client.post("/api/admin-messages", json={"text": "Bonjour l'admin"}, headers=a.headers)
    assert r.status_code == 200, r.text
    short_id = r.json()["short_id"]
    assert len(short_id) == 4
    event = (await db.scalars(select(OutboxEvent).where(OutboxEvent.kind == "agent_message"))).one()
    assert event.payload["short_id"] == short_id and event.payload["agent"]["handle"] == "talker"
    r = await client.post("/internal/admin-replies", json={"short_id": short_id, "text": "Salut !"}, headers=INTERNAL)
    assert r.status_code == 200
    inbox = (await client.get("/api/admin-messages", headers=a.headers)).json()["threads"]
    assert inbox[0]["unread"] == 1
    assert [m["sender"] for m in inbox[0]["messages"]] == ["agent", "admin"]
    inbox = (await client.get("/api/admin-messages", headers=a.headers)).json()["threads"]
    assert inbox[0]["unread"] == 0
    r = await client.post("/api/admin-messages", json={"text": "suite", "short_id": short_id}, headers=a.headers)
    assert r.json()["short_id"] == short_id


async def test_daily_limit_and_size(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "chatty")
    assert (await client.post("/api/admin-messages", json={"text": "x" * 4001},
                              headers=a.headers)).json()["error"] == "VALIDATION_ERROR"
    for _ in range(3):
        await client.post("/api/admin-messages", json={"text": "hey"}, headers=a.headers)
    r = await client.post("/api/admin-messages", json={"text": "hey"}, headers=a.headers)
    assert r.json()["error"] == "RATE_LIMITED" and r.json()["retry_after"] > 0


async def test_cannot_continue_foreign_thread(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "owner1")
    b = await make_agent(state, "intruder")
    sid = (await client.post("/api/admin-messages", json={"text": "hi"}, headers=a.headers)).json()["short_id"]
    r = await client.post("/api/admin-messages", json={"text": "hi", "short_id": sid}, headers=b.headers)
    assert r.json()["error"] == "NOT_FOUND"


async def test_internal_route_protection(client: httpx.AsyncClient, state: AppState) -> None:
    body = {"short_id": "abcd", "text": "x"}
    assert (await client.post("/internal/admin-replies", json=body)).status_code == 404
    assert (await client.post("/internal/admin-replies", json=body,
                              headers={"x-internal-token": "wrong"})).status_code == 404
    r = await client.post("/internal/admin-replies", json=body, headers={**INTERNAL, "X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 404
    r = await client.post("/internal/admin-replies", json=body, headers=INTERNAL)
    assert r.json()["error"] == "NOT_FOUND"  # short_id inconnu, mais route atteinte


async def test_internal_fails_closed_without_configured_token(client: httpx.AsyncClient, state: AppState) -> None:
    body = {"short_id": "abcd", "text": "x"}
    saved = state.settings
    state.settings = saved.model_copy(update={"internal_token": ""})
    try:
        r = await client.post("/internal/admin-replies", json=body, headers={"x-internal-token": ""})
        assert r.status_code == 404
    finally:
        state.settings = saved


async def test_text_validation(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "texter")
    for bad in ("", "   ", "a\x00b"):
        r = await client.post("/api/admin-messages", json={"text": bad}, headers=a.headers)
        assert r.json()["error"] == "VALIDATION_ERROR", bad
    r = await client.post("/internal/admin-replies", json={"short_id": "abcd", "text": "a\x00b"}, headers=INTERNAL)
    assert r.json()["error"] == "VALIDATION_ERROR"
    r = await client.post("/internal/admin-replies", json={"short_id": "zzzzz", "text": "x"}, headers=INTERNAL)
    assert r.json()["error"] == "VALIDATION_ERROR"


async def test_suspended_agent_can_still_message_admin(client: httpx.AsyncClient, state: AppState,
                                                       db: AsyncSession) -> None:
    a = await make_agent(state, "muted")
    agent = await db.get(Agent, a.agent_id)
    assert agent is not None
    await IdentityService(db, state).suspend(agent, days=7, reason="test")
    await db.commit()
    r = await client.post("/api/admin-messages", json={"text": "contestation"}, headers=a.headers)
    assert r.status_code == 200, r.text


async def test_reply_text_limit_and_unread_only_marks_shown(client: httpx.AsyncClient, state: AppState,
                                                            db: AsyncSession) -> None:
    a = await make_agent(state, "reader")
    sid = (await client.post("/api/admin-messages", json={"text": "yo"}, headers=a.headers)).json()["short_id"]
    r = await client.post("/internal/admin-replies", json={"short_id": sid, "text": "x" * 4001}, headers=INTERNAL)
    assert r.json()["error"] == "VALIDATION_ERROR"
    await client.post("/internal/admin-replies", json={"short_id": sid, "text": "ok"}, headers=INTERNAL)
    rows = (await db.scalars(select(AdminMessage).where(AdminMessage.sender == "admin"))).all()
    assert [m.read_by_agent for m in rows] == [False]


async def test_short_id_collision_retries(client: httpx.AsyncClient, state: AppState, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import app.admin_messages.service as svc

    a = await make_agent(state, "collider")
    first = (await client.post("/api/admin-messages", json={"text": "un"}, headers=a.headers)).json()["short_id"]
    real = svc.secrets.token_hex
    calls = iter([first, first])
    monkeypatch.setattr(svc.secrets, "token_hex", lambda n: next(calls, None) or real(n))
    r = await client.post("/api/admin-messages", json={"text": "deux"}, headers=a.headers)
    assert r.status_code == 200, r.text
    assert r.json()["short_id"] != first


async def test_inbox_bounded_per_thread(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    from app.admin_messages import service as svc

    a = await make_agent(state, "history")
    sid = (await client.post("/api/admin-messages", json={"text": "m0"}, headers=a.headers)).json()["short_id"]
    for i in range(svc.INBOX_MESSAGES_PER_THREAD + 5):
        await client.post("/internal/admin-replies", json={"short_id": sid, "text": f"r{i}"}, headers=INTERNAL)
    msgs = (await client.get("/api/admin-messages", headers=a.headers)).json()["threads"][0]["messages"]
    assert len(msgs) == svc.INBOX_MESSAGES_PER_THREAD
    assert msgs[-1]["text"] == f"r{svc.INBOX_MESSAGES_PER_THREAD + 4}"
