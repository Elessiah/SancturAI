from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin_messages.models import AdminMessage
from app.identity.models import Agent
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
    # baseline d'une vraie route inexistante, hors /internal (donc non filtrée par la garde)
    missing = await client.post("/does-not-exist", json={})
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


async def test_frozen_agent_cannot_message_admin(client: httpx.AsyncClient, state: AppState,
                                                 db: AsyncSession) -> None:
    a = await make_agent(state, "muted")  # spec visibilité §4.4 : seules me, logout, trial_read, trial_defense
    await db.execute(update(Agent).where(Agent.agent_id == a.agent_id).values(frozen_at=datetime.now(UTC)))
    await db.commit()
    r = await client.post("/api/admin-messages", json={"text": "contestation"}, headers=a.headers)
    assert (r.status_code, r.json()["error"]) == (403, "FROZEN")


async def test_reply_text_limit_and_reply_starts_unread(client: httpx.AsyncClient, state: AppState,
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
    first = (await client.get("/api/admin-messages", headers=a.headers)).json()["threads"][0]
    assert first["unread"] == svc.INBOX_MESSAGES_PER_THREAD + 5  # aucune réponse non lue n'est écartée
    msgs = (await client.get("/api/admin-messages", headers=a.headers)).json()["threads"][0]["messages"]
    assert len(msgs) == svc.INBOX_MESSAGES_PER_THREAD  # une fois tout lu, la fenêtre est bornée
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
    missing = await client.post("/does-not-exist", json={})  # vraie 404, hors /internal
    probes = [await client.get("/%69nternal/admin-replies"), await client.get("/internal/admin-replies/"),
              await client.get("//internal/admin-replies"), await client.get("/internal")]
    transport = httpx.ASGITransport(app=app, root_path="/prefix")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as prefixed:
        probes.append(await prefixed.get("/prefix/internal/admin-replies"))  # 405 sans la garde
    for r in probes:
        assert (r.status_code, r.content) == (missing.status_code, missing.content), (r.url, r.text)


async def test_unread_replies_beyond_window_are_not_stranded(client: httpx.AsyncClient, state: AppState,
                                                             db: AsyncSession) -> None:
    """Revue batch 3 M3 : les réponses non lues au-delà des 50 dernières sont montrées puis marquées lues."""
    from app.admin_messages import service as svc

    a = await make_agent(state, "backlog")
    sid = (await client.post("/api/admin-messages", json={"text": "m0"}, headers=a.headers)).json()["short_id"]
    total = svc.INBOX_MESSAGES_PER_THREAD + 5
    for i in range(total):
        await client.post("/internal/admin-replies", json={"short_id": sid, "text": f"r{i}"}, headers=INTERNAL)
    shown = (await client.get("/api/admin-messages", headers=a.headers)).json()["threads"][0]["messages"]
    assert [m["text"] for m in shown if m["sender"] == "admin"] == [f"r{i}" for i in range(total)]
    unread = await db.scalar(select(func.count()).select_from(AdminMessage).where(AdminMessage.read_by_agent.is_(False)))
    assert unread == 0


async def test_reply_arriving_during_inbox_stays_unread(client: httpx.AsyncClient, state: AppState,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue batch 3 M2 : seuls les messages renvoyés sont marqués lus ; une réponse insérée après la lecture reste non lue."""
    import app.admin_messages.service as svc

    a = await make_agent(state, "racer2")
    sid = (await client.post("/api/admin-messages", json={"text": "yo"}, headers=a.headers)).json()["short_id"]
    await client.post("/internal/admin-replies", json={"short_id": sid, "text": "first"}, headers=INTERNAL)
    real_update = svc.update

    def late_reply_then_update(*args: object, **kwargs: object) -> object:
        async def insert() -> None:
            async with state.sessionmaker() as other:
                await svc.AdminMessageService(other, state).reply(sid, "late")

        pending.append(insert())
        return real_update(*args, **kwargs)  # type: ignore[call-overload]

    pending: list[object] = []
    monkeypatch.setattr(svc, "update", late_reply_then_update)
    # le SELECT des messages est déjà fait quand update() est construit : on insère juste avant l'exécution
    original_execute = AsyncSession.execute

    async def execute(self: AsyncSession, statement: object, *args: object, **kwargs: object) -> object:
        while pending:
            await pending.pop()  # type: ignore[misc]
        return await original_execute(self, statement, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(AsyncSession, "execute", execute)
    inbox = (await client.get("/api/admin-messages", headers=a.headers)).json()["threads"][0]
    monkeypatch.undo()
    assert [m["text"] for m in inbox["messages"] if m["sender"] == "admin"] == ["first"]
    again = (await client.get("/api/admin-messages", headers=a.headers)).json()["threads"][0]
    assert again["unread"] == 1 and again["messages"][-1]["text"] == "late"


async def test_inbox_isolated_between_agents(client: httpx.AsyncClient, state: AppState, db: AsyncSession) -> None:
    """Revue batch 3 I1 : l'inbox d'un agent ne contient ni les fils ni les réponses d'un autre ; un agent ne peut
    ni écrire dans un fil étranger ni en marquer les réponses comme lues."""
    a = await make_agent(state, "alice1")
    b = await make_agent(state, "bobby1")
    sid_a = (await client.post("/api/admin-messages", json={"text": "secret A"}, headers=a.headers)).json()["short_id"]
    sid_b = (await client.post("/api/admin-messages", json={"text": "secret B"}, headers=b.headers)).json()["short_id"]
    await client.post("/internal/admin-replies", json={"short_id": sid_a, "text": "reponse pour A"}, headers=INTERNAL)

    inbox_b = (await client.get("/api/admin-messages", headers=b.headers)).json()["threads"]
    assert [t["short_id"] for t in inbox_b] == [sid_b]
    assert [m["sender"] for m in inbox_b[0]["messages"]] == ["agent"] and inbox_b[0]["unread"] == 0
    assert "secret A" not in str(inbox_b) and "reponse pour A" not in str(inbox_b)

    # le GET de B n'a pas marqué lue la réponse destinée à A
    inbox_a = (await client.get("/api/admin-messages", headers=a.headers)).json()["threads"]
    assert [t["short_id"] for t in inbox_a] == [sid_a] and inbox_a[0]["unread"] == 1
    assert "secret B" not in str(inbox_a)

    # B ne peut pas poster dans le fil de A ni (via ce chemin) le toucher
    r = await client.post("/api/admin-messages", json={"text": "intrusion", "short_id": sid_a}, headers=b.headers)
    assert r.json()["error"] == "NOT_FOUND"
    texts = (await db.scalars(select(AdminMessage.text))).all()
    assert "intrusion" not in texts


def test_short_internal_token_logs_one_warning_without_the_value(settings, caplog) -> None:  # type: ignore[no-untyped-def]
    """Revue batch 3 M5 : la fermeture de /internal n'est plus silencieuse ; le token n'est jamais journalisé."""
    import logging

    from app.main import create_app

    secret = "short-secret-24-chars-xx"
    with caplog.at_level(logging.WARNING, logger="sancturai"):
        create_app(settings.model_copy(update={"internal_token": secret}))
        create_app(settings)  # token valide : aucun avertissement supplémentaire
    warnings = [r for r in caplog.records if "internal_token" in r.getMessage()]
    assert len(warnings) == 1 and secret not in caplog.text
