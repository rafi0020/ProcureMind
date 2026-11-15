"""Approvals: actions the agent proposed that wait for an administrator."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.agent.events import publish
from app.api.deps import audit, require_role
from app.db import tenant_session
from app.models.identity import User
from app.models.runs import Approval, Run
from app.security.sessions import Principal
from app.worker.run import wake_workers

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=500)


@router.get("")
async def list_approvals(
    user: Principal = Depends(require_role("clerk", "approver", "admin")),
    status: Literal["pending", "approved", "rejected", "all"] = "pending",
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    stmt = (
        select(Approval, Run.question, User.email)
        .join(Run, Run.id == Approval.run_id)
        .outerjoin(User, User.id == Approval.requested_by)
        .order_by(Approval.created_at.desc())
        .limit(limit)
    )
    if status != "all":
        stmt = stmt.where(Approval.status == status)
    if user.role != "admin":  # clerks see what they asked for; administrators see everything
        stmt = stmt.where(Approval.requested_by == user.id)
    async with tenant_session(user.org_id) as db:
        rows = (await db.execute(stmt)).all()
    return {
        "approvals": [
            {
                "id": str(a.id),
                "run_id": str(a.run_id),
                "question": question,
                "tool": a.tool,
                "arguments": a.arguments,
                "status": a.status,
                "reason": a.reason,
                "requested_by": email,
                "created_at": a.created_at.isoformat(),
                "decided_at": a.decided_at.isoformat() if a.decided_at else None,
            }  # fmt: skip
            for a, question, email in rows
        ]
    }


@router.post("/{approval_id}/decide")
async def decide(
    approval_id: UUID, body: Decision, request: Request, admin: Principal = Depends(require_role("approver", "admin"))
) -> dict:
    async with tenant_session(admin.org_id) as db:
        approval = (
            await db.execute(select(Approval).where(Approval.id == approval_id).with_for_update())
        ).scalar_one_or_none()
        if approval is None:
            raise HTTPException(404, "Approval not found.")
        if approval.status != "pending":
            raise HTTPException(409, f"This request was already {approval.status}.")
        run = (await db.execute(select(Run).where(Run.id == approval.run_id).with_for_update())).scalar_one()
        sole_admin = False
        if run.user_id == admin.id:
            # Two people: whoever started the run does not approve what it proposes, unless the organisation has
            # only one person who may decide (an approver or an administrator), and that is recorded.
            deciders = await db.scalar(
                select(func.count()).select_from(User).where(User.role.in_(("approver", "admin")), User.is_active)
            )
            if deciders > 1:
                raise HTTPException(403, "Someone other than the person who started this investigation must decide it.")
            sole_admin = True
        approval.status, approval.reason = body.decision, (body.reason or "").strip() or None
        approval.decided_by, approval.decided_at = admin.id, func.now()
        requeued = run.status == "waiting_approval"
        if requeued:  # hand the run back to the workers; it carries out or drops the action and continues
            run.status, run.pending, run.attempts = "queued", None, 0
        audit(
            db, admin, f"approval.{body.decision}", str(approval.id),
            {"tool": approval.tool, "arguments": approval.arguments, "run_id": str(run.id), "reason": approval.reason,
             "sole_admin": sole_admin},
            request,
        )  # fmt: skip
        run_id = run.id
    if requeued:
        await publish(run_id, "run.status", {"status": "queued"})
        await wake_workers()
    return {"status": body.decision}
