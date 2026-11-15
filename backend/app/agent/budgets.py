"""Limits on what one run, and one organisation per day, may consume.

An agent decides its own next step, so nothing in its instructions can be relied on to make it stop.
These limits are checked by the runtime before every step and end the run with status `budget_exceeded`,
keeping whatever evidence it has gathered.
"""

from sqlalchemy import text

from app.config import get_settings
from app.db import system_session, tenant_session
from app.models.runs import Run

# Everything spent today: agent runs, and model calls made outside a run (document extraction).
SPENT_TODAY = """
    SELECT coalesce(sum(spend.cost_usd), 0) FROM (
        SELECT cost_usd, created_at FROM runs
        UNION ALL
        SELECT cost_usd, created_at FROM model_spend
    ) AS spend WHERE spend.created_at >= date_trunc('day', now())
"""


class BudgetExceeded(Exception):
    def __init__(self, which: str, message: str):
        super().__init__(message)
        self.which = which


async def check_budget(run: Run, elapsed_ms_now: int = 0) -> None:
    s = get_settings()
    if run.steps_used >= s.run_max_steps:
        raise BudgetExceeded("steps", f"Stopped: the limit of {s.run_max_steps} model steps was reached.")
    if run.tokens_used >= s.run_max_tokens:
        raise BudgetExceeded("tokens", f"Stopped: the limit of {s.run_max_tokens:,} tokens was reached.")
    if (run.active_ms + elapsed_ms_now) / 1000 >= s.run_max_seconds:
        raise BudgetExceeded("time", f"Stopped: the time limit of {s.run_max_seconds} seconds was reached.")
    if float(run.cost_usd) >= s.run_max_cost_usd:
        raise BudgetExceeded("cost", f"Stopped: the cost limit of ${s.run_max_cost_usd:.2f} for one run was reached.")
    reached = await daily_limit_reached(run.org_id)
    if reached == "global":
        raise BudgetExceeded(
            "global", f"Stopped: this installation's daily limit of ${s.global_daily_cost_usd:.2f} was reached."
        )
    if reached == "org_daily":
        raise BudgetExceeded(
            "org_daily", f"Stopped: the organisation's daily limit of ${s.org_daily_cost_usd:.2f} was reached."
        )


async def daily_limit_reached(org_id) -> str | None:
    """'global' or 'org_daily' when today's spend has reached a daily limit, else None. Shared by agent runs and by
    document extraction, so one budget covers every model call."""
    s = get_settings()
    async with tenant_session(org_id) as db:
        spent = await db.scalar(text(SPENT_TODAY))  # row-level security: this organisation's rows only
    async with system_session() as db:
        everyone = await db.scalar(text(SPENT_TODAY))  # the system session sees every organisation
    if float(everyone) >= s.global_daily_cost_usd:
        return "global"
    if float(spent) >= s.org_daily_cost_usd:
        return "org_daily"
    return None
