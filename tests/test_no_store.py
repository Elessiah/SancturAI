import httpx

from app.state import AppState
from tests.helpers import make_agent


async def test_every_api_response_is_no_store(client: httpx.AsyncClient, state: AppState) -> None:
    a = await make_agent(state, "nostore")
    await client.put("/api/vault/n.md", json={"content": "x"}, headers=a.headers)
    for method, url in (("GET", "/api/vault"), ("GET", "/api/vault/n.md"), ("GET", "/api/me"),
                        ("GET", "/api/vault/missing.md"), ("GET", "/api/nope")):
        r = await client.request(method, url, headers=a.headers)
        assert r.headers.get("cache-control") == "no-store", url
    r = await client.get("/mcp/")
    assert r.headers.get("cache-control") == "no-store"
    assert (await client.get("/healthz")).headers.get("cache-control") is None
