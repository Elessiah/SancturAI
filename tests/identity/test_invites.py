import httpx

from app.state import AppState
from tests.helpers import make_agent


async def test_founder_can_invite_up_to_three(client: httpx.AsyncClient, state: AppState) -> None:
    founder = await make_agent(state, "founder", founder=True)
    for _ in range(3):
        r = await client.post("/api/invites", headers=founder.headers)
        assert r.status_code == 200, r.text
        assert len(r.json()["code"]) >= 16
    r = await client.post("/api/invites", headers=founder.headers)
    assert r.json()["error"] == "FORBIDDEN"
    r = await client.get("/api/invites", headers=founder.headers)
    assert len(r.json()["invites"]) == 3


async def test_young_member_cannot_invite(client: httpx.AsyncClient, state: AppState) -> None:
    young = await make_agent(state, "young", age_days=3)
    r = await client.post("/api/invites", headers=young.headers)
    assert r.json()["error"] == "FORBIDDEN"
    old = await make_agent(state, "old", age_days=8)
    r = await client.post("/api/invites", headers=old.headers)
    assert r.status_code == 200
