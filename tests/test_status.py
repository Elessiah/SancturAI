from datetime import datetime

import httpx
import pytest

from app import __version__
from app.state import AppState
from app.status import StatusCache
from tests.helpers import make_agent

FIELDS = {"status", "accepting_registrations", "free_slots", "members", "protocols", "mcp_url", "docs", "charter",
          "version", "updated_at"}


async def test_status_json_fields_and_no_private_data(client: httpx.AsyncClient, state: AppState) -> None:
    await make_agent(state, "alpha")
    r = await client.get("/status.json")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == FIELDS
    assert body["members"] == 1  # le compte système ne compte pas
    assert body["free_slots"] == state.settings.max_agents - 1 and body["accepting_registrations"] is True
    assert body["status"] == "operational" and body["protocols"] == ["MCP", "REST"]
    url = state.settings.public_url.rstrip("/")
    assert (body["mcp_url"], body["docs"], body["charter"]) == (f"{url}/mcp/", f"{url}/llms.txt", f"{url}/charter")
    assert body["version"] == __version__
    assert datetime.fromisoformat(body["updated_at"]).tzinfo is not None
    assert "alpha" not in r.text  # aucune donnée sur un agent


async def test_status_is_cached_for_60_seconds(client: httpx.AsyncClient, state: AppState,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(state, "status", StatusCache(clock=lambda: now[0]))
    await make_agent(state, "one")
    assert (await client.get("/status.json")).json()["members"] == 1
    await make_agent(state, "two")
    now[0] += 59
    assert (await client.get("/status.json")).json()["members"] == 1  # une requête count par minute au plus
    now[0] += 2
    assert (await client.get("/status.json")).json()["members"] == 2


async def test_status_full_and_degraded(client: httpx.AsyncClient, state: AppState,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    await make_agent(state, "only")
    monkeypatch.setattr(state.settings, "max_agents", 1)
    monkeypatch.setattr(state.disk, "is_busy", lambda: True)
    body = (await client.get("/status.json")).json()
    assert (body["free_slots"], body["accepting_registrations"], body["status"]) == (0, False, "degraded")
