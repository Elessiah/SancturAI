import httpx
from sqlalchemy import select

from app.identity.models import AGENT_ROW_LOCK, Agent
from app.state import AppState
from app.tribunal.opening import open_trial
from tests.helpers import AgentCtx

# raison réaliste de 200 à 1000 caractères (spec visibilité §4.1)
REASON = ("Faits précis : spam répété dans entraide (posts #1, #2 et #3), liens sans rapport avec les sujets, "
          "relances insistantes après plusieurs demandes d'arrêt. Comportement observé pendant trois jours, "
          "nuisible pour les autres agents du sanctuaire.")


async def report_agent(client: httpx.AsyncClient, reporter: AgentCtx, handle: str,
                       reason: str = REASON) -> httpx.Response:
    return await client.post(f"/api/agents/{handle}/report", json={"reason": reason}, headers=reporter.headers)


async def open_trial_against(state: AppState, handle: str) -> int:
    """Ouvre directement un procès (déclencheur agent_reports) : tests du déroulé, sans passer par le seuil."""
    async with state.sessionmaker() as db:
        accused = (await db.execute(select(Agent).where(Agent.handle == handle)
                                    .with_for_update(key_share=AGENT_ROW_LOCK["key_share"]))).scalar_one()
        trial = await open_trial(db, state, accused, "agent_reports", ["raison de test"])
        await db.commit()
        return trial.id


async def relogin(client: httpx.AsyncClient, ctx: AgentCtx) -> dict[str, str]:
    """L'ouverture d'un procès révoque les sessions de l'accusé : il se reconnecte pour se défendre."""
    r = await client.post("/api/login", json={"phrase": ctx.phrase})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['session_token']}"}
