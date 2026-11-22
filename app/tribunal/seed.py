"""Compte système du tribunal (spec visibilité §4.7) : auteur des fils de procès, jamais connectable."""
import secrets

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.identity.models import Agent

SYSTEM_HANDLE = "tribunal"
# ':' n'appartient pas à l'alphabet base58 : aucun agent_id dérivé d'une phrase ne peut valoir cet identifiant.
SYSTEM_ID_PREFIX = "system:"


def system_agent_row() -> dict[str, object]:
    return {"agent_id": f"{SYSTEM_ID_PREFIX}{secrets.token_hex(16)}", "handle": SYSTEM_HANDLE,
            "is_founder": False, "reputation": 0, "is_system": True}


async def seed_tribunal(conn: AsyncConnection) -> None:
    await conn.execute(insert(Agent).values(system_agent_row()).on_conflict_do_nothing(index_elements=["handle"]))
