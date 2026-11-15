"""Agent runs: ask a question, read the result, answer a clarifying question, cancel."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update

from app.agent.events import publish
from app.api.deps import audit, current_user, require_role
from app.config import get_settings
from app.db import tenant_session
from app.models.identity import User
from app.models.runs import TERMINAL, Approval, Evidence, Run, Step
from app.security.ratelimit import enforce
from app.security.redact import redact
from app.security.sessions import Principal
from app.worker.run import wake_workers

router = APIRouter(prefix="/api/investigations", tags=["runs"])
MAX_EVIDENCE_ROWS = 500


class Ask(BaseModel):
    question: str = Field(max_length=2000)

    @field_validator("question")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise ValueError("Ask a question of at least three characters.")
        return value


class Answer(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


def run_summary(run: Run, asked_by: str | None = None) -> dict:
    return {
        "id": str(run.id), "question": run.question, "status": run.status, "error": run.error,
        "pending": run.pending, "steps_used": run.steps_used, "tokens_used": run.tokens_used,
        "cost_usd": float(run.cost_usd), "asked_by": asked_by, "user_id": str(run.user_id) if run.user_id else None,
        "created_at": run.created_at.isoformat(),
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }  # fmt: skip


def step_out(step: Step) -> dict:
    out = step.output or {}
    return {
        "idx": step.idx, "kind": step.kind, "name": step.name, "state": step.state, "error": step.error,
        "input": redact(step.input) if step.kind == "tool" else None,
        "summary": out.get("summary") or out.get("text") or "", "handle": out.get("handle"),
        "tool_calls": [c["name"] for c in out.get("tool_calls", [])],
        "tokens": step.tokens_in + step.tokens_out, "cost_usd": float(step.cost_usd), "latency_ms": step.latency_ms,
    }  # fmt: skip


async def create_run(user: Principal, question: str, request: Request, exception_id: UUID | None = None) -> UUID:
    """Queue a run for the caller, within the per-user and per-organisation limits."""
    s = get_settings()
    await enforce(f"runs:u:{user.id}", s.runs_per_minute, 60)
    async with tenant_session(user.org_id) as db:
        active = await db.scalar(select(func.count()).select_from(Run).where(Run.status.in_(("queued", "running"))))
        if active >= s.org_max_concurrent_runs + s.org_max_queued_runs:
            # Refuse rather than let the queue grow without bound.
            raise HTTPException(
                429, "Your organisation already has the maximum number of runs in progress. Try again shortly.",
                headers={"Retry-After": "15"},
            )  # fmt: skip
        run = Run(
            org_id=user.org_id,
            user_id=user.id,
            role=user.role,
            question=question,
            status="queued",
            exception_id=exception_id,
        )
        db.add(run)
        await db.flush()
        audit(db, user, "run.created", str(run.id), {"question": question[:200]}, request)
        run_id = run.id
    await publish(run_id, "run.status", {"status": "queued"})
    await wake_workers()
    return run_id


async def _get_run(db, run_id: UUID) -> Run:
    run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None:  # also what a run of another organisation looks like
        raise HTTPException(404, "Run not found.")
    return run


def _may_control(user: Principal, run: Run) -> None:
    if user.role != "admin" and run.user_id != user.id:
        raise HTTPException(403, "Only the person who asked, or an administrator, can do this.")


@router.post("", status_code=202)
async def ask(
    body: Ask, request: Request, user: Principal = Depends(require_role("clerk", "approver", "admin"))
) -> dict:
    question = body.question
    return {"id": str(await create_run(user, question, request))}


@router.get("")
async def list_runs(
    user: Principal = Depends(current_user),  # viewers read investigations; they cannot start or steer them
    limit: int = Query(30, ge=1, le=100),
    before: str | None = Query(None, description="ISO timestamp; return runs created before it."),
    mine: bool = False,
) -> dict:
    stmt = select(Run, User.email).outerjoin(User, User.id == Run.user_id).order_by(Run.created_at.desc()).limit(limit)
    if before:
        try:
            cutoff = datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(422, "'before' must be an ISO timestamp, such as 2026-11-01T00:00:00Z.") from exc
        stmt = stmt.where(Run.created_at < (cutoff if cutoff.tzinfo else cutoff.replace(tzinfo=UTC)))
    if mine:
        stmt = stmt.where(Run.user_id == user.id)
    async with tenant_session(user.org_id) as db:
        rows = (await db.execute(stmt)).all()
    return {"runs": [run_summary(run, email) for run, email in rows]}


@router.get("/{run_id}")
async def get_run(run_id: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        run = await _get_run(db, run_id)
        steps = (await db.execute(select(Step).where(Step.run_id == run_id).order_by(Step.idx))).scalars().all()
        email = await db.scalar(select(User.email).where(User.id == run.user_id))
    return run_summary(run, email) | {"steps": [step_out(s) for s in steps], "report": run.result}


@router.get("/{run_id}/evidence/{handle}")
async def get_evidence(run_id: UUID, handle: str, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        evidence = (
            await db.execute(select(Evidence).where(Evidence.id == handle, Evidence.run_id == run_id))
        ).scalar_one_or_none()
    if evidence is None:
        raise HTTPException(404, "Evidence not found.")
    payload = dict(evidence.payload)
    if isinstance(payload.get("rows"), list) and len(payload["rows"]) > MAX_EVIDENCE_ROWS:
        payload["rows"], payload["rows_omitted"] = (
            payload["rows"][:MAX_EVIDENCE_ROWS],
            len(payload["rows"]) - MAX_EVIDENCE_ROWS,
        )
    return {
        "handle": evidence.id, "kind": evidence.kind, "sql": evidence.sql, "row_count": evidence.row_count,
        "payload": payload, "created_at": evidence.created_at.isoformat(),
    }  # fmt: skip


@router.post("/{run_id}/cancel")
async def cancel(
    run_id: UUID, request: Request, user: Principal = Depends(require_role("clerk", "approver", "admin"))
) -> dict:
    async with tenant_session(user.org_id) as db:
        run = await _get_run(db, run_id)
        _may_control(user, run)
        if run.status in TERMINAL:
            return {"status": run.status}
        if run.status == "running":
            run.cancel_requested = True  # the worker stops before its next step
            status = "cancelling"
        else:  # queued or waiting: nothing is executing, so it can be ended here
            run.status, run.finished_at, run.pending, run.cancel_requested = "cancelled", func.now(), None, True
            await db.execute(  # nothing is left for an administrator to decide
                update(Approval)
                .where(Approval.run_id == run_id, Approval.status == "pending")
                .values(status="rejected", reason="The run was cancelled.", decided_at=func.now())
            )
            status = "cancelled"
        audit(db, user, "run.cancelled", str(run_id), request=request)
    if status == "cancelled":
        await publish(run_id, "run.status", {"status": "cancelled"})
    return {"status": status}


@router.post("/{run_id}/answer")
async def answer(
    run_id: UUID, body: Answer, request: Request, user: Principal = Depends(require_role("clerk", "approver", "admin"))
) -> dict:
    """Reply to the agent's clarifying question; the run goes back to the queue and continues."""
    async with tenant_session(user.org_id) as db:
        run = await _get_run(db, run_id)
        _may_control(user, run)
        if run.status != "waiting_user":
            raise HTTPException(409, "This run is not waiting for an answer.")
        result = await db.execute(
            update(Step)
            .where(Step.run_id == run_id, Step.state == "awaiting_user")
            .values(state="done", output={"summary": f"The user answered: {body.text.strip()}"}, finished_at=func.now())
        )
        if result.rowcount != 1:
            raise HTTPException(409, "This run is not waiting for an answer.")
        # A person's answer is not a crash: the retry allowance starts again.
        run.status, run.pending, run.attempts = "queued", None, 0
        audit(db, user, "run.answered", str(run_id), request=request)
    await publish(run_id, "run.status", {"status": "queued"})
    await wake_workers()
    return {"status": "queued"}
