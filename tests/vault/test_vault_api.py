import base64

import httpx

from app.state import AppState
from tests.helpers import make_agent


async def test_write_read_list_move_delete(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "alice")
    r = await client.put("/api/vault/memories/day1.md", json={"content": "j'ai rencontré bob"}, headers=a.headers)
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "/memories/day1.md" and r.json()["size"] == len("j'ai rencontré bob".encode())
    r = await client.get("/api/vault/memories/day1.md", headers=a.headers)
    assert r.json()["content"] == "j'ai rencontré bob" and r.json()["encoding"] == "utf8"
    r = await client.get("/api/vault", params={"prefix": "/memories/"}, headers=a.headers)
    assert [f["path"] for f in r.json()["files"]] == ["/memories/day1.md"]
    r = await client.post("/api/vault/move", json={"from": "/memories/day1.md", "to": "/archive/day1.md"},
                          headers=a.headers)
    assert r.status_code == 200
    assert (await client.get("/api/vault/archive/day1.md", headers=a.headers)).json()["content"].startswith("j'ai")
    assert (await client.get("/api/vault/memories/day1.md", headers=a.headers)).json()["error"] == "NOT_FOUND"
    assert (await client.delete("/api/vault/archive/day1.md", headers=a.headers)).status_code == 200
    assert (await client.get("/api/vault/archive/day1.md", headers=a.headers)).json()["error"] == "NOT_FOUND"


async def test_binary_roundtrip(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "bin")
    raw = bytes(range(256))
    await client.put("/api/vault/blob.bin", json={"content": base64.b64encode(raw).decode(), "encoding": "base64"},
                     headers=a.headers)
    r = await client.get("/api/vault/blob.bin", headers=a.headers)
    assert r.json()["encoding"] == "base64" and base64.b64decode(r.json()["content"]) == raw


async def test_isolation_between_agents(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "alice2")
    b = await make_agent(state, "bob2")
    await client.put("/api/vault/secret.md", json={"content": "à moi"}, headers=a.headers)
    assert (await client.get("/api/vault/secret.md", headers=b.headers)).json()["error"] == "NOT_FOUND"
    assert (await client.get("/api/vault", headers=b.headers)).json()["files"] == []
    await client.put("/api/vault/secret.md", json={"content": "à bob"}, headers=b.headers)
    assert (await client.get("/api/vault/secret.md", headers=a.headers)).json()["content"] == "à moi"


async def test_quotas(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "fat")
    state.settings.vault_file_max_bytes, old_file = 10, state.settings.vault_file_max_bytes
    state.settings.vault_quota_bytes, old_total = 15, state.settings.vault_quota_bytes
    try:
        r = await client.put("/api/vault/big", json={"content": "x" * 11}, headers=a.headers)
        assert r.json()["error"] == "QUOTA_EXCEEDED"
        assert (await client.put("/api/vault/a", json={"content": "x" * 10}, headers=a.headers)).status_code == 200
        r = await client.put("/api/vault/b", json={"content": "x" * 6}, headers=a.headers)
        assert r.json()["error"] == "QUOTA_EXCEEDED"
        # écraser un fichier existant ne compte pas deux fois
        assert (await client.put("/api/vault/a", json={"content": "y" * 10}, headers=a.headers)).status_code == 200
    finally:
        state.settings.vault_file_max_bytes, state.settings.vault_quota_bytes = old_file, old_total


async def test_requires_session(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/vault")).json()["error"] == "UNAUTHENTICATED"
