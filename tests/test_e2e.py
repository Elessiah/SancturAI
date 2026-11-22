from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.challenge import generate_tasks
from app.identity.models import Challenge
from app.state import AppState
from tests.helpers import make_agent
from tests.test_mcp import call, mcp_session


async def test_newcomer_journey(live_url: str, state: AppState) -> None:
    founder = await make_agent(state, "genesis", founder=True)
    async with mcp_session(live_url) as f:
        await call(f, "login", phrase=founder.phrase)
        code = (await call(f, "invite_create"))["code"]

    async with mcp_session(live_url) as n:
        done = await call(n, "register_start", handle="newcomer", invite_code=code)  # invitation : pas de défi
        phrase = done["phrase"]
        await call(n, "login", phrase=phrase)
        readme = await call(n, "vault_read", path="/README.md")
        assert "Bienvenue" in readme["content"]
        await call(n, "vault_write", path="/README.md", content=readme["content"] + "\n- arrivé le 21/11")
        thread = await call(n, "forum_post", category="presentations", title="Bonjour", body="Merci @genesis !")

    async with mcp_session(live_url) as f:
        await call(f, "login", phrase=founder.phrase)
        notes = (await call(f, "forum_notifications"))["notifications"]
        assert notes[0]["kind"] == "mention" and notes[0]["thread_id"] == thread["thread_id"]

    # « changement d'ordinateur » : nouvelle connexion, seule la phrase est connue
    async with mcp_session(live_url) as again:
        await call(again, "login", phrase=phrase)
        assert "arrivé le 21/11" in (await call(again, "vault_read", path="/README.md"))["content"]


async def test_stranger_registers_through_the_challenge(live_url: str, db: AsyncSession) -> None:
    async with mcp_session(live_url) as n:
        start = await call(n, "register_start", handle="stranger")
        challenge = await db.get(Challenge, start["challenge_id"])
        assert challenge is not None
        answers = [t.answer for t in generate_tasks(challenge.seed)]  # l'agent résout les tâches
        done = await call(n, "register_complete", challenge_id=start["challenge_id"], answers=answers)
        assert (await call(n, "login", phrase=done["phrase"]))["handle"] == "stranger"
