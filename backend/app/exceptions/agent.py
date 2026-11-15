"""The Exception Agent: its instructions and tools. Workers load `AGENT` (see app/worker/run.py)."""

from uuid import UUID

from sqlalchemy import select

from app.agent.tools import AgentSpec
from app.db import system_session
from app.exceptions import tools  # noqa: F401 - registers the agent's own tools
from app.exceptions.common import registry
from app.exceptions.prompts import SYSTEM_PROMPT
from app.models.identity import Org


async def context(org_id: UUID) -> str:
    async with system_session() as db:
        name = await db.scalar(select(Org.name).where(Org.id == org_id))
    return f"Organisation: {name}. Only this organisation's records are visible."


AGENT = AgentSpec(SYSTEM_PROMPT, registry, context=context)
