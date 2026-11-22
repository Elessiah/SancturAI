import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin_messages.models import AdminMessage
from app.identity.models import Agent
from app.identity.service import IdentityService
from app.outbox.models import OutboxEvent
from app.state import AppState
from tests.helpers import make_agent

INTERNAL = {"x-internal-token": "test-internal-token-0123456789abcdef"}


async def test_message_reply_inbox(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    a = await make_agent(state, "talker")
    r = await client.post("/api/admin-messages", json={"text": "Bonjour l'admin"}, headers=a.headers)
    assert r.status_code == 200, r.text
    short_id = r.json()["short_id"]
    assert len(short_id) == 6
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
    body = {"short_id": "abcdef", "text": "x"}
    assert (await client.post("/internal/admin-replies", json=body)).status_code == 404
    assert (await client.post("/internal/admin-replies", json=body,
                              headers={"x-internal-token": "wrong"})).status_code == 404
    r = await client.post("/internal/admin-replies", json=body, headers={**INTERNAL, "X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 404
    r = await client.post("/internal/admin-replies", json=body, headers=INTERNAL)
    assert r.json()["error"] == "NOT_FOUND"  # short_id inconnu, mais route atteinte


async def test_internal_guard_reveals_nothing(client: httpx.AsyncClient) -> None:
    """Sans token : même réponse qu'une route inexistante, quels que soient méthode, corps ou chemin (revue S1)."""
    missing = await client.post("/internal/does-not-exist", json={})
    assert missing.status_code == 404
    probes = [
        await client.post("/internal/admin-replies", json={"short_id": "abcdef", "text": "x"}),
        await client.get("/internal/admin-replies"),  # 405 sinon
        await client.post("/internal/admin-replies", content=b"{bad", headers={"content-type": "application/json"}),
        await client.post("/internal/admin-replies", json={"short_id": "zz"}),
        await client.post("/internal/admin-replies", content=b"{bad",
                          headers={**INTERNAL, "X-Forwarded-For": "1.2.3.4", "content-type": "application/json"}),
        await client.post("/internal/future-route", json={}, headers={"x-internal-token": "wrong"}),
    ]
    for r in probes:
        assert (r.status_code, r.content) == (missing.status_code, missing.content), r.text


async def test_internal_fails_closed_without_configured_token(client: httpx.AsyncClient, state: AppState) -> None:
    body = {"short_id": "abcdef", "text": "x"}
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
    r = await client.post("/internal/admin-replies", json={"short_id": "abcdef", "text": "a\x00b"}, headers=INTERNAL)
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


async def test_create_thread_only_retries_short_id_collisions(state: AppState, db: AsyncSession) -> None:
    import pytest
    from sqlalchemy.exc import IntegrityError

    from app.admin_messages.service import AdminMessageService

    ghost = Agent(agent_id="ghost-agent", handle="ghost")  # absent de la base : violation de FK, pas de short_id
    with pytest.raises(IntegrityError):
        await AdminMessageService(db, state)._create_thread(ghost)


async def test_short_id_is_exactly_six_hex(client: httpx.AsyncClient, state: AppState) -> None:
    """Revue S2 : 6 hexadécimaux (16,7 M fils) ; l'ancien format à 4 est refusé."""
    a = await make_agent(state, "sixhex")
    for bad in ("abcd", "abcde", "abcdef0", "ABCDEF"):
        r = await client.post("/internal/admin-replies", json={"short_id": bad, "text": "x"}, headers=INTERNAL)
        assert r.json()["error"] == "VALIDATION_ERROR", bad
        r = await client.post("/api/admin-messages", json={"text": "x", "short_id": bad}, headers=a.headers)
        assert r.json()["error"] == "VALIDATION_ERROR", bad


async def test_daily_limit_holds_under_concurrency(client: httpx.AsyncClient, state: AppState) -> None:
    """Revue S3 : le compteur DB, sous verrou par agent, fait foi (limiteur mémoire vide après redémarrage)."""
    import asyncio

    a = await make_agent(state, "racer")
    rs = await asyncio.gather(*(client.post("/api/admin-messages", json={"text": f"m{i}"}, headers=a.headers)
                                for i in range(6)))
    assert sorted(r.status_code for r in rs).count(200) == 3, [r.text for r in rs]
    assert all(r.json()["error"] == "RATE_LIMITED" for r in rs if r.status_code != 200)


async def test_internal_fails_closed_with_short_configured_token(client: httpx.AsyncClient, state: AppState) -> None:
    """Revue S4 : un token configuré de moins de 32 caractères ferme /internal, même présenté exactement."""
    saved = state.settings
    short = "x" * 31
    state.settings = saved.model_copy(update={"internal_token": short})
    try:
        r = await client.post("/internal/admin-replies", json={"short_id": "abcdef", "text": "x"},
                              headers={"x-internal-token": short})
        assert r.status_code == 404
    finally:
        state.settings = saved


async def test_internal_guard_path_variants(app, client: httpx.AsyncClient) -> None:  # type: ignore[no-untyped-def]
    """Revue S5 : variantes encodées, slash final et root_path ne contournent pas la garde."""
    missing = await client.post("/internal/does-not-exist", json={})
    probes = [await client.get("/%69nternal/admin-replies"), await client.get("/internal/admin-replies/"),
              await client.get("//internal/admin-replies"), await client.get("/internal")]
    transport = httpx.ASGITransport(app=app, root_path="/prefix")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as prefixed:
        probes.append(await prefixed.get("/prefix/internal/admin-replies"))  # 405 sans la garde
    for r in probes:
        assert (r.status_code, r.content) == (missing.status_code, missing.content), (r.url, r.text)
