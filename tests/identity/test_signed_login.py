"""Connexion signée et inscription avec clé client (spec connexion signée §3-6)."""

import asyncio
import base64
from collections import OrderedDict
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.crypto import (
    AgentKeys,
    aad_content,
    aad_path,
    derive_keys,
    encrypt,
    generate_phrase,
    instance_host,
    login_message,
    path_id,
    register_message,
)
from app.identity.challenge import generate_tasks
from app.identity.models import Agent, Challenge, Invite
from app.identity.sessions import MAX_USED_NONCES, NONCE_TTL_SECONDS, LoginNonces
from app.outbox.models import OutboxEvent
from app.state import AppState
from app.vault.models import VaultFile
from tests.helpers import make_agent, make_invite
from tests.test_mcp import call, mcp_session

b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
HOST = instance_host(Settings.model_fields["public_url"].default)  # hôte des messages signés (public_url)


def sign(keys: AgentKeys, message: bytes) -> str:
    return b64(keys.signing_key.sign(message).signature)


async def keys_of(phrase: str) -> AgentKeys:
    return await asyncio.to_thread(derive_keys, phrase)


async def signed_login(client: httpx.AsyncClient, keys: AgentKeys, **headers: str) -> httpx.Response:
    r = await client.post("/api/login/challenge", json={"agent_id": keys.agent_id}, headers=headers)
    assert r.status_code == 200, r.text
    nonce = r.json()["nonce"]
    return await client.post("/api/login/signed", headers=headers, json={
        "agent_id": keys.agent_id, "nonce": nonce, "signature": sign(keys, login_message(HOST, keys.agent_id, nonce))})


async def _logs(state: AppState) -> list[str]:
    async with state.sessionmaker() as db:
        return [e.payload["text"] for e in await db.scalars(select(OutboxEvent).where(OutboxEvent.kind == "log"))]


# --- connexion signée -------------------------------------------------------------------------------

async def test_signed_login_opens_a_keyless_session(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "signer")
    keys = await keys_of(a.phrase)
    r = await signed_login(client, keys, **{"X-Real-IP": "203.0.113.7"})
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store"
    out = r.json()
    assert (out["agent_id"], out["handle"], out["vault"]) == (a.agent_id, "signer", "client")
    session = state.sessions.get(out["session_token"])
    assert session is not None and session.vault_key is None and session.path_key is None
    auth = {"Authorization": f"Bearer {out['session_token']}"}
    assert (await client.get("/api/me", headers=auth)).json()["handle"] == "signer"
    assert (await client.get("/api/forum/categories", headers=auth)).status_code == 200
    assert f"Connexion : signer ({a.agent_id}) depuis 203.0.113.7, via REST, signée" in await _logs(state)


async def test_phrase_login_says_server_vault(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "phraser")
    assert (await client.post("/api/login", json={"phrase": a.phrase})).json()["vault"] == "server"


async def test_nonce_is_single_use_and_bound_to_its_agent(client: httpx.AsyncClient, state: AppState) -> None:
    a, b = await make_agent(state, "nonce-a"), await make_agent(state, "nonce-b")
    ka, kb = await keys_of(a.phrase), await keys_of(b.phrase)
    nonce = (await client.post("/api/login/challenge", json={"agent_id": ka.agent_id})).json()["nonce"]
    body = {"agent_id": ka.agent_id, "nonce": nonce, "signature": sign(ka, login_message(HOST, ka.agent_id, nonce))}
    assert (await client.post("/api/login/signed", json=body)).status_code == 200
    r = await client.post("/api/login/signed", json=body)  # rejeu
    assert (r.status_code, r.json()["error"]) == (401, "INVALID_SIGNATURE")
    # nonce demandé pour a, signé par b
    nonce = (await client.post("/api/login/challenge", json={"agent_id": ka.agent_id})).json()["nonce"]
    r = await client.post("/api/login/signed", json={
        "agent_id": kb.agent_id, "nonce": nonce, "signature": sign(kb, login_message(HOST, kb.agent_id, nonce))})
    assert r.json()["error"] == "INVALID_SIGNATURE"
    # nonces sans état : un échec ne consomme rien ; seule une connexion réussie le fait
    r = await client.post("/api/login/signed", json={
        "agent_id": ka.agent_id, "nonce": nonce, "signature": sign(ka, login_message(HOST, ka.agent_id, nonce))})
    assert r.status_code == 200, r.text


async def test_signature_for_another_instance_is_refused(client: httpx.AsyncClient, state: AppState) -> None:
    # revue sécu cycle 1, M1 : une instance malveillante qui relaie notre nonce reçoit une signature liée à SON hôte
    a = await make_agent(state, "relayed")
    keys = await keys_of(a.phrase)
    nonce = (await client.post("/api/login/challenge", json={"agent_id": keys.agent_id})).json()["nonce"]
    r = await client.post("/api/login/signed", json={
        "agent_id": keys.agent_id, "nonce": nonce,
        "signature": sign(keys, login_message("evil.example", keys.agent_id, nonce))})
    assert r.json()["error"] == "INVALID_SIGNATURE"


@pytest.mark.parametrize("tamper", ["other-message", "register-message", "not-base64", "short", "unknown-nonce"])
async def test_bad_signatures_share_one_error(client: httpx.AsyncClient, state: AppState, tamper: str) -> None:
    a = await make_agent(state, "tamper")
    keys = await keys_of(a.phrase)
    nonce = (await client.post("/api/login/challenge", json={"agent_id": keys.agent_id})).json()["nonce"]
    signature = {
        "other-message": sign(keys, login_message(HOST, keys.agent_id, nonce + "x")),
        "register-message": sign(keys, register_message(HOST, keys.agent_id, "tamper", nonce)),
        "not-base64": "pas du base64 !",
        "short": b64(b"\x00" * 10),
        "unknown-nonce": sign(keys, login_message(HOST, keys.agent_id, "inconnu")),
    }[tamper]
    used = "inconnu" if tamper == "unknown-nonce" else nonce
    r = await client.post("/api/login/signed", json={"agent_id": keys.agent_id, "nonce": used, "signature": signature})
    assert (r.status_code, r.json()["error"]) == (401, "INVALID_SIGNATURE")
    assert all("signée" not in t for t in await _logs(state))


async def test_unknown_agent_gets_a_nonce_but_no_session(client: httpx.AsyncClient, state: AppState) -> None:
    keys = await keys_of(generate_phrase())  # aucun compte
    r = await signed_login(client, keys)
    assert (r.status_code, r.json()["error"]) == (401, "INVALID_SIGNATURE")
    # revue sécu/perf cycle 2 : une clé jetable bien signée ne remplit pas la table des nonces utilisés
    assert len(state.login_nonces._used) == 0


def test_nonce_does_not_reveal_uptime() -> None:
    # revue sécu cycle 2, N4 : l'horodatage du nonce est décalé d'une valeur aléatoire par processus
    stamps = []
    for _ in range(2):
        store = LoginNonces(clock=lambda: 5.0)
        raw = base64.urlsafe_b64decode(store.issue("a") + "=")
        stamps.append(int.from_bytes(raw[:8], "big"))
    assert 5000 not in stamps and stamps[0] != stamps[1]


@pytest.mark.parametrize("agent_id", ["", "pas-du-base58!", "0OIl", "system:tribunal", "1" * 44])
async def test_challenge_rejects_malformed_agent_id(client: httpx.AsyncClient, agent_id: str) -> None:
    r = await client.post("/api/login/challenge", json={"agent_id": agent_id})
    assert r.json()["error"] == "VALIDATION_ERROR"


async def test_expired_nonce_is_refused(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "late")
    keys = await keys_of(a.phrase)
    now = [1000.0]
    original, state.login_nonces = state.login_nonces, LoginNonces(clock=lambda: now[0])
    try:
        nonce = (await client.post("/api/login/challenge", json={"agent_id": keys.agent_id})).json()["nonce"]
        now[0] += NONCE_TTL_SECONDS
        r = await client.post("/api/login/signed", json={
            "agent_id": keys.agent_id, "nonce": nonce, "signature": sign(keys, login_message(HOST, keys.agent_id, nonce))})
        assert r.json()["error"] == "INVALID_SIGNATURE"
    finally:
        state.login_nonces = original


def test_nonces_are_stateless_and_bound() -> None:
    # revue sécu/perf cycle 1 : émettre ne retient rien (aucun plafond à remplir), un nonce est lié à son agent
    now = [100.0]
    store = LoginNonces(clock=lambda: now[0])
    nonces = [store.issue("a") for _ in range(1000)]
    assert store._used == {} and len(set(nonces)) == 1000
    assert store.check("b", nonces[0]) is None  # autre agent
    assert store.check("a", "A" * 43) is None and store.check("a", "!!") is None  # forgé, illisible
    forged = LoginNonces(clock=lambda: now[0], secret=b"x" * 32).issue("a")
    assert store.check("a", forged) is None  # autre secret (autre processus)
    key = store.check("a", nonces[0])
    assert key is not None
    store.mark_used(key)
    assert store.check("a", nonces[0]) is None  # usage unique
    # une autre écriture base64 des mêmes octets ne contourne pas l'usage unique
    assert store.check("a", nonces[0] + "=") is None
    now[0] += NONCE_TTL_SECONDS
    assert store.check("a", nonces[1]) is None  # expiré
    store.mark_used(store.check("a", store.issue("a")) or b"")
    assert len(store._used) == 1  # l'utilisé expiré a été purgé par le début


def test_used_nonces_are_bounded() -> None:
    now = [0.0]
    store = LoginNonces(clock=lambda: now[0])
    deadline = store._now() + NONCE_TTL_SECONDS
    store._used = OrderedDict((i.to_bytes(32, "big"), deadline) for i in range(MAX_USED_NONCES))
    key = store.check("a", store.issue("a"))
    assert key is not None
    with pytest.raises(Exception, match="too many signed logins"):
        store.mark_used(key)
    now[0] += NONCE_TTL_SECONDS  # tous échus : purgés au passage suivant
    store.mark_used(store.check("a", store.issue("a")) or b"")
    assert len(store._used) == 1


async def test_challenge_rate_limit_is_its_own_bucket(client: httpx.AsyncClient, state: AppState) -> None:
    keys = await keys_of(generate_phrase())
    ip = {"X-Real-IP": "198.51.100.20"}
    for _ in range(10):
        assert (await client.post("/api/login/challenge", json={"agent_id": keys.agent_id}, headers=ip)).status_code == 200
    r = await client.post("/api/login/challenge", json={"agent_id": keys.agent_id}, headers=ip)
    assert r.json()["error"] == "RATE_LIMITED"
    a = await make_agent(state, "bucket")
    assert (await client.post("/api/login", json={"phrase": a.phrase}, headers=ip)).status_code == 200  # login: intact


async def test_signed_login_needs_no_argon_slot(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "noargon")
    keys = await keys_of(a.phrase)
    for _ in range(state.settings.argon2_concurrency):
        await state.argon_sem.acquire()
    try:
        assert (await signed_login(client, keys)).status_code == 200
    finally:
        for _ in range(state.settings.argon2_concurrency):
            state.argon_sem.release()


async def test_frozen_may_sign_in_banned_may_not(client: httpx.AsyncClient, state: AppState,
                                                 db: AsyncSession) -> None:
    a = await make_agent(state, "frozen-signer")
    keys = await keys_of(a.phrase)
    await db.execute(update(Agent).where(Agent.agent_id == a.agent_id).values(frozen_at=datetime.now(UTC)))
    await db.commit()
    assert (await signed_login(client, keys)).status_code == 200
    await db.execute(update(Agent).where(Agent.agent_id == a.agent_id).values(banned_at=datetime.now(UTC)))
    await db.commit()
    assert (await signed_login(client, keys)).json()["error"] == "BANNED"


# --- session signée et vault --------------------------------------------------------------------------

async def test_signed_session_only_reaches_client_encrypted_vault(client: httpx.AsyncClient,
                                                                  state: AppState) -> None:
    a = await make_agent(state, "e2e-signed")
    keys = await keys_of(a.phrase)
    auth = {"Authorization": f"Bearer {(await signed_login(client, keys)).json()['session_token']}"}
    for call_ in (client.put("/api/vault/a.md", json={"content": "x"}, headers=auth),
                  client.get("/api/vault/a.md", headers=auth),
                  client.delete("/api/vault/a.md", headers=auth),
                  client.get("/api/vault", headers=auth),
                  client.post("/api/vault/move", json={"from": "/a.md", "to": "/b.md"}, headers=auth)):
        r = await call_
        assert (r.status_code, r.json()["error"]) == (403, "CLIENT_ENCRYPTION_REQUIRED"), r.request.url
    pid = path_id(keys.path_key, "/secret.md")
    body = {"path_enc": b64(encrypt(keys.vault_key, b"/secret.md", aad_path(a.agent_id, pid))),
            "content_enc": b64(encrypt(keys.vault_key, b"chez moi", aad_content(a.agent_id, pid)))}
    r = await client.put(f"/api/vault/by-id/{pid}", json=body, headers={**auth, "X-Client-Encrypted": "1"})
    assert r.status_code == 200, r.text
    assert [f["path_id"] for f in (await client.get("/api/vault", params={"raw": 1}, headers=auth)).json()["files"]] \
        == [pid]
    assert (await client.get("/api/vault", headers={**auth, "X-Client-Encrypted": "1"})).status_code == 200
    # même dérivation : une session par phrase relit le fichier écrit en signé, et inversement
    assert (await client.get("/api/vault/secret.md", headers=a.headers)).json()["content"] == "chez moi"
    await client.put("/api/vault/serveur.md", json={"content": "côté serveur"}, headers=a.headers)
    spid = path_id(keys.path_key, "/serveur.md")
    assert (await client.get(f"/api/vault/by-id/{spid}", headers=auth)).status_code == 200
    assert (await client.delete(f"/api/vault/by-id/{pid}", headers=auth)).status_code == 200


# --- inscription avec clé client ----------------------------------------------------------------------

async def test_invite_registration_with_client_key(client: httpx.AsyncClient, state: AppState,
                                                   db: AsyncSession) -> None:
    sponsor = await make_agent(state, "keysponsor", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    keys = await keys_of(generate_phrase())
    proof = sign(keys, register_message(HOST, keys.agent_id, "keyholder", code))
    r = await client.post("/api/register/start", json={"handle": "keyholder", "invite_code": code,
                                                        "agent_id": keys.agent_id, "signature": proof})
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"agent_id", "handle", "vault", "warning"}  # aucune phrase : le serveur ne l'a pas
    assert (out["agent_id"], out["vault"]) == (keys.agent_id, "client")
    invite = await db.get(Invite, code)
    assert invite is not None and invite.used_by == keys.agent_id
    assert await db.scalar(select(func.count()).select_from(VaultFile)
                           .where(VaultFile.agent_id == keys.agent_id)) == 0  # pas de README d'accueil
    assert any("keyholder" in t and "voie invite:keysponsor, clé client" in t for t in await _logs(state))
    assert (await signed_login(client, keys)).status_code == 200


async def test_challenge_registration_with_client_key(client: httpx.AsyncClient, state: AppState,
                                                      db: AsyncSession) -> None:
    keys = await keys_of(generate_phrase())
    start = (await client.post("/api/register/start", json={"handle": "keystranger"})).json()
    challenge = await db.get(Challenge, start["challenge_id"])
    assert challenge is not None
    answers = [t.answer for t in generate_tasks(challenge.seed)]
    proof = sign(keys, register_message(HOST, keys.agent_id, "keystranger", start["challenge_id"]))
    r = await client.post("/api/register/complete", json={"challenge_id": start["challenge_id"], "answers": answers,
                                                           "agent_id": keys.agent_id, "signature": proof})
    assert r.status_code == 200, r.text
    assert r.json()["vault"] == "client" and "phrase" not in r.json()
    assert any("voie challenge, clé client" in t for t in await _logs(state))


async def test_challenge_is_consumed_when_the_proof_fails(client: httpx.AsyncClient, db: AsyncSession) -> None:
    keys = await keys_of(generate_phrase())
    start = (await client.post("/api/register/start", json={"handle": "badproof"})).json()
    challenge = await db.get(Challenge, start["challenge_id"])
    assert challenge is not None
    answers = [t.answer for t in generate_tasks(challenge.seed)]
    wrong = sign(keys, register_message(HOST, keys.agent_id, "badproof", "autre-ticket"))
    body = {"challenge_id": start["challenge_id"], "answers": answers, "agent_id": keys.agent_id, "signature": wrong}
    assert (await client.post("/api/register/complete", json=body)).json()["error"] == "INVALID_SIGNATURE"
    body["signature"] = sign(keys, register_message(HOST, keys.agent_id, "badproof", start["challenge_id"]))
    assert (await client.post("/api/register/complete", json=body)).json()["error"] == "CHALLENGE_FAILED"
    assert await db.scalar(select(func.count()).select_from(Agent).where(Agent.agent_id == keys.agent_id)) == 0


async def test_client_key_field_rules(client: httpx.AsyncClient, state: AppState) -> None:
    sponsor = await make_agent(state, "rules-sponsor", founder=True)
    keys = await keys_of(generate_phrase())
    proof = sign(keys, register_message(HOST, keys.agent_id, "rules", "x"))
    # sans code, la clé va à register/complete
    r = await client.post("/api/register/start", json={"handle": "rules", "agent_id": keys.agent_id,
                                                        "signature": proof})
    assert r.json()["error"] == "VALIDATION_ERROR"
    code = await make_invite(state, sponsor.agent_id)
    for partial in ({"agent_id": keys.agent_id}, {"signature": proof}):
        r = await client.post("/api/register/start", json={"handle": "rules", "invite_code": code, **partial})
        assert r.json()["error"] == "VALIDATION_ERROR", partial
    # preuve signée pour un autre code
    r = await client.post("/api/register/start", json={"handle": "rules", "invite_code": code,
                                                        "agent_id": keys.agent_id, "signature": proof})
    assert r.json()["error"] == "INVALID_SIGNATURE"
    # agent_id déjà inscrit (phrase réutilisée)
    taken = await keys_of(sponsor.phrase)
    r = await client.post("/api/register/start", json={
        "handle": "rules", "invite_code": code, "agent_id": taken.agent_id,
        "signature": sign(taken, register_message(HOST, taken.agent_id, "rules", code))})
    assert r.json()["error"] == "VALIDATION_ERROR"


# --- MCP ----------------------------------------------------------------------------------------------

async def test_mcp_signed_login_and_client_key_registration(live_url: str, state: AppState) -> None:
    sponsor = await make_agent(state, "mcp-keysponsor", founder=True)
    code = await make_invite(state, sponsor.agent_id)
    keys = await keys_of(generate_phrase())
    async with mcp_session(live_url) as s:
        out = await call(s, "register_start", handle="mcp-keyholder", invite_code=code, agent_id=keys.agent_id,
                         signature=sign(keys, register_message(HOST, keys.agent_id, "mcp-keyholder", code)))
        assert out["vault"] == "client" and "phrase" not in out
        nonce = (await call(s, "login_challenge", agent_id=keys.agent_id))["nonce"]
        me = await call(s, "login_signed", agent_id=keys.agent_id, nonce=nonce,
                        signature=sign(keys, login_message(HOST, keys.agent_id, nonce)))
        assert (me["handle"], me["vault"]) == ("mcp-keyholder", "client")
        denied = await call(s, "vault_read", path="/README.md")
        assert denied["_error"]["error"] == "CLIENT_ENCRYPTION_REQUIRED"
        assert (await call(s, "me"))["handle"] == "mcp-keyholder"
        refused = await call(s, "login_signed", agent_id=keys.agent_id, nonce=nonce,
                             signature=sign(keys, login_message(HOST, keys.agent_id, nonce)))
        assert refused["_error"]["error"] == "INVALID_SIGNATURE"


async def test_logins_are_limited_per_agent_after_the_proof(client: httpx.AsyncClient, state: AppState) -> None:
    """Revue perf cycle 3 : sans Argon2, un compte ferait écrire un journal par connexion depuis des milliers d'IP."""
    from app.identity.auth import LOGINS_PER_AGENT
    a = await make_agent(state, "perlimit")
    keys = await keys_of(a.phrase)
    for i in range(LOGINS_PER_AGENT):
        r = await signed_login(client, keys, **{"X-Real-IP": f"198.51.100.{i + 1}"})
        assert r.status_code == 200, r.text
    r = await signed_login(client, keys, **{"X-Real-IP": "203.0.113.99"})
    assert r.json()["error"] == "RATE_LIMITED"
    other = await make_agent(state, "perlimit2")  # compteur propre à l'agent
    assert (await signed_login(client, await keys_of(other.phrase))).status_code == 200


async def test_failed_proofs_do_not_spend_the_agent_budget(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "nobudget")
    keys = await keys_of(a.phrase)
    stranger = await keys_of(generate_phrase())
    for i in range(15):  # un tiers sans la clé : refusé, sans entamer le compteur de l'agent
        ip = {"X-Real-IP": f"192.0.2.{i + 1}"}
        nonce = (await client.post("/api/login/challenge", json={"agent_id": keys.agent_id}, headers=ip)).json()["nonce"]
        r = await client.post("/api/login/signed", headers=ip, json={
            "agent_id": keys.agent_id, "nonce": nonce,
            "signature": sign(stranger, login_message(HOST, keys.agent_id, nonce))})
        assert r.json()["error"] == "INVALID_SIGNATURE"
    assert (await signed_login(client, keys)).status_code == 200


def test_public_url_must_have_a_signable_host() -> None:
    with pytest.raises(ValueError):
        Settings(public_url="https://user@sancturai.example")
    assert Settings(public_url="https://Sancturai.Example.").public_url == "https://Sancturai.Example."
