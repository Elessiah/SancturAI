import base64

import httpx

from app.crypto import aad_content, aad_path, derive_keys, encrypt, path_id
from app.state import AppState
from app.vault.paths import MAX_PATH
from app.vault.service import MAX_PATH_ENC_BYTES
from tests.helpers import make_agent

b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731


async def test_client_encrypted_file_is_compatible(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "e2e")
    keys = derive_keys(a.phrase)
    pid = path_id(keys.path_key, "/secret.md")
    body = {"path_enc": b64(encrypt(keys.vault_key, b"/secret.md", aad_path(a.agent_id, pid))),
            "content_enc": b64(encrypt(keys.vault_key, b"chiffre chez moi", aad_content(a.agent_id, pid)))}
    r = await client.put(f"/api/vault/by-id/{pid}", json=body, headers=a.headers)
    assert r.json()["error"] == "VALIDATION_ERROR"  # en-tête manquant
    r = await client.put(f"/api/vault/by-id/{pid}", json=body, headers={**a.headers, "X-Client-Encrypted": "1"})
    assert r.status_code == 200, r.text
    assert r.json()["size"] == len(b"chiffre chez moi")
    # compatibilité : le mode serveur relit le fichier
    r = await client.get("/api/vault/secret.md", headers=a.headers)
    assert r.json()["content"] == "chiffre chez moi"
    r = await client.get(f"/api/vault/by-id/{pid}", headers=a.headers)
    assert base64.b64decode(r.json()["content_enc"]) == base64.b64decode(body["content_enc"])
    r = await client.get("/api/vault", params={"raw": 1}, headers=a.headers)
    assert [f["path_id"] for f in r.json()["files"]] == [pid]
    assert (await client.delete(f"/api/vault/by-id/{pid}", headers=a.headers)).status_code == 200


async def test_bad_path_id_rejected(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "e2e-bad")
    r = await client.put("/api/vault/by-id/nothex", json={"path_enc": "AA==", "content_enc": "AA=="},
                         headers={**a.headers, "X-Client-Encrypted": "1"})
    assert r.json()["error"] == "VALIDATION_ERROR"
    assert (await client.get("/api/vault/by-id/nothex", headers=a.headers)).json()["error"] == "VALIDATION_ERROR"


async def test_missing_and_oversized_raw(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "e2e-edge")
    pid = "a" * 64
    assert (await client.get(f"/api/vault/by-id/{pid}", headers=a.headers)).json()["error"] == "NOT_FOUND"
    assert (await client.delete(f"/api/vault/by-id/{pid}", headers=a.headers)).json()["error"] == "NOT_FOUND"
    hdr = {**a.headers, "X-Client-Encrypted": "1"}
    r = await client.put(f"/api/vault/by-id/{pid}", json={"path_enc": b64(b"x" * (MAX_PATH_ENC_BYTES + 1)),
                                                                 "content_enc": "AA=="},
                         headers=hdr)
    assert r.json()["error"] == "VALIDATION_ERROR"
    big = b64(b"x" * (state.settings.vault_file_max_bytes + 41))
    r = await client.put(f"/api/vault/by-id/{pid}", json={"path_enc": "AA==", "content_enc": big}, headers=hdr)
    assert r.json()["error"] == "QUOTA_EXCEEDED"
    r = await client.put(f"/api/vault/by-id/{pid}", json={"path_enc": "AA==", "content_enc": "AA=="}, headers=hdr)
    assert r.status_code == 200 and r.json()["size"] == 0  # plancher à 0


LONG_PATH = "/" + "😀" * (MAX_PATH - 1)  # 512 caractères, 4 octets UTF-8 chacun (hors "/")


async def test_longest_non_ascii_path_e2e(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "e2e-long")
    keys = derive_keys(a.phrase)
    pid = path_id(keys.path_key, LONG_PATH)
    body = {"path_enc": b64(encrypt(keys.vault_key, LONG_PATH.encode(), aad_path(a.agent_id, pid))),
            "content_enc": b64(encrypt(keys.vault_key, b"x", aad_content(a.agent_id, pid)))}
    r = await client.put(f"/api/vault/by-id/{pid}", json=body, headers={**a.headers, "X-Client-Encrypted": "1"})
    assert r.status_code == 200, r.text
    r = await client.get("/api/vault", headers=a.headers)
    assert [f["path"] for f in r.json()["files"] if f["path_id"] == pid] == [LONG_PATH]


async def test_server_blob_of_long_path_can_be_reput(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "e2e-reput")
    assert (await client.put(f"/api/vault{LONG_PATH}", json={"content": "v1"}, headers=a.headers)).status_code == 200
    pid = (await client.get("/api/vault", params={"raw": 1}, headers=a.headers)).json()["files"][0]["path_id"]
    raw = (await client.get(f"/api/vault/by-id/{pid}", headers=a.headers)).json()
    r = await client.put(f"/api/vault/by-id/{pid}", json={"path_enc": raw["path_enc"], "content_enc": raw["content_enc"]},
                         headers={**a.headers, "X-Client-Encrypted": "1"})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/api/vault{LONG_PATH}", headers=a.headers)).json()["content"] == "v1"


async def test_by_id_isolated_between_agents(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "e2e-owner")
    b = await make_agent(state, "e2e-other")
    assert (await client.put("/api/vault/mine.md", json={"content": "secret"}, headers=a.headers)).status_code == 200
    pid = (await client.get("/api/vault", params={"raw": 1}, headers=a.headers)).json()["files"][0]["path_id"]
    assert (await client.get(f"/api/vault/by-id/{pid}", headers=b.headers)).json()["error"] == "NOT_FOUND"
    assert (await client.delete(f"/api/vault/by-id/{pid}", headers=b.headers)).json()["error"] == "NOT_FOUND"
    assert (await client.get("/api/vault", params={"raw": 1}, headers=b.headers)).json()["files"] == []
    # un PUT de b sur ce path_id crée un fichier de b, sans toucher celui de a
    r = await client.put(f"/api/vault/by-id/{pid}", json={"path_enc": "AA==", "content_enc": "AA=="},
                         headers={**b.headers, "X-Client-Encrypted": "1"})
    assert r.status_code == 200
    assert (await client.get("/api/vault/mine.md", headers=a.headers)).json()["content"] == "secret"


async def test_by_id_prefix_reserved_in_server_mode(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "e2e-reserved")
    r = await client.put("/api/vault/by-id/notes/a.md", json={"content": "x"}, headers=a.headers)
    assert r.json()["error"] == "VALIDATION_ERROR"
    assert (await client.put("/api/vault/a.md", json={"content": "x"}, headers=a.headers)).status_code == 200
    r = await client.post("/api/vault/move", json={"from": "/a.md", "to": "/by-id/x.md"}, headers=a.headers)
    assert r.json()["error"] == "VALIDATION_ERROR"
