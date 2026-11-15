"""Model usage and estimated cost. Administrators see the organisation; clerks see their own."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from app.agent.budgets import SPENT_TODAY
from app.api.deps import require_role
from app.config import get_settings
from app.db import tenant_session
from app.security.sessions import Principal

router = APIRouter(prefix="/api/usage", tags=["usage"])

GROUP_KEYS = {
    "user": "coalesce(u.email, CASE WHEN s.run_id IS NULL THEN '(document extraction)' ELSE '(deleted user)' END)",
    "model": "s.name",
    "day": "to_char(date_trunc('day', s.created_at), 'YYYY-MM-DD')",
}
# Agent model steps, and model calls made for documents (extraction), which belong to no run.
QUERY = """
SELECT {key} AS key, count(*) AS model_calls, count(DISTINCT s.run_id) AS runs,
       coalesce(sum(s.tokens_in), 0) AS tokens_in, coalesce(sum(s.tokens_out), 0) AS tokens_out,
       coalesce(sum(s.cost_usd), 0) AS cost_usd, coalesce(round(avg(s.latency_ms)), 0) AS avg_latency_ms
FROM (
    SELECT st.run_id, st.name, st.created_at, st.tokens_in, st.tokens_out, st.cost_usd, st.latency_ms, r.user_id
    FROM steps st JOIN runs r ON r.id = st.run_id WHERE st.kind = 'model'
    UNION ALL
    SELECT NULL::uuid, m.model, m.created_at, m.tokens_in, m.tokens_out, m.cost_usd, m.latency_ms, d.uploaded_by
    FROM model_spend m LEFT JOIN documents d ON d.id = m.document_id
) s LEFT JOIN users u ON u.id = s.user_id
WHERE s.created_at >= :start AND s.created_at < :end
  AND (CAST(:user_id AS uuid) IS NULL OR s.user_id = :user_id)
GROUP BY 1 ORDER BY {order}
"""


@router.get("")
async def usage(
    user: Principal = Depends(require_role("clerk", "approver", "admin")),
    group: Literal["user", "model", "day"] = "day",
    start: datetime | None = Query(None, alias="from"),
    end: datetime | None = Query(None, alias="to"),
) -> dict:
    end = end or datetime.now(UTC) + timedelta(minutes=1)
    start = start or end - timedelta(days=30)
    if start >= end or end - start > timedelta(days=366):
        raise HTTPException(422, "Choose a range of at most one year, with 'from' before 'to'.")
    only_user = None if user.role == "admin" else user.id
    sql = QUERY.format(key=GROUP_KEYS[group], order="1" if group == "day" else "6 DESC")  # noqa: S608 - fixed fragments
    async with tenant_session(user.org_id) as db:
        rows = (await db.execute(text(sql), {"start": start, "end": end, "user_id": only_user})).all()
        today = await db.scalar(text(SPENT_TODAY))
    items = [
        {
            "key": r.key,
            "model_calls": r.model_calls,
            "runs": r.runs,
            "tokens_in": int(r.tokens_in),
            "tokens_out": int(r.tokens_out),
            "cost_usd": round(float(r.cost_usd), 6),
            "avg_latency_ms": int(r.avg_latency_ms),
        }  # fmt: skip
        for r in rows
    ]
    s = get_settings()
    return {
        "group": group,
        "scope": "organisation" if only_user is None else "you",
        "from": start.isoformat(),
        "to": end.isoformat(),
        "items": items,
        "totals": {
            "model_calls": sum(i["model_calls"] for i in items),
            "tokens_in": sum(i["tokens_in"] for i in items),
            "tokens_out": sum(i["tokens_out"] for i in items),
            "cost_usd": round(sum(i["cost_usd"] for i in items), 6),
        },
        "budget": {
            "org_daily_cost_usd": s.org_daily_cost_usd,
            "org_spent_today_usd": round(float(today), 6) if only_user is None else None,
            "run_max_cost_usd": s.run_max_cost_usd,
            "run_max_steps": s.run_max_steps,
            "cost_is_estimate": True,
        },
    }
