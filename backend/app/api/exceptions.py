"""Exceptions: the queue of invoices a person must decide on, and resolving them."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.deps import audit, current_user, require_role
from app.db import tenant_session
from app.models.ap import Invoice
from app.security.sessions import Principal

router = APIRouter(prefix="/api/exceptions", tags=["exceptions"])
Outcome = Literal[
    "approve_with_variance", "hold", "request_credit_note", "request_corrected_invoice", "reject_duplicate", "escalate"
]
OPEN = ("open", "investigating", "recommended", "escalated")
INVOICE_STATE = {"approve_with_variance": "pending_approval", "hold": "on_hold", "request_credit_note": "on_hold",
                 "request_corrected_invoice": "on_hold", "reject_duplicate": "rejected"}  # fmt: skip


class Resolve(BaseModel):
    outcome: Outcome
    note: str | None = Field(default=None, max_length=1000)


SELECT = """
    SELECT e.*, i.invoice_number, i.vendor_id, i.total, i.currency, i.kind, i.status AS invoice_status, v.name AS vendor_name
    FROM exceptions e JOIN invoices i ON i.id = e.invoice_id LEFT JOIN vendors v ON v.id = i.vendor_id
"""


def exception_out(r) -> dict:
    return {"id": str(r.id), "invoice_id": str(r.invoice_id), "status": r.status, "reasons": r.reasons, "findings": r.findings,
            "recommendation": r.recommendation, "resolution": r.resolution, "note": r.note,
            "resolved_by": str(r.resolved_by) if r.resolved_by else None, "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "created_at": r.created_at.isoformat(),
            "invoice": {"number": r.invoice_number, "vendor_id": str(r.vendor_id) if r.vendor_id else None, "vendor": r.vendor_name,
                        "total": None if r.total is None else f"{r.total:.2f}", "currency": r.currency, "kind": r.kind, "status": r.invoice_status}}  # fmt: skip


@router.get("")
async def list_exceptions(
    user: Principal = Depends(current_user), status: str | None = Query(None, max_length=20)
) -> dict:
    where, params = "", {}
    if status == "open":
        where, params = " WHERE e.status = ANY(:s)", {"s": list(OPEN)}
    elif status:
        where, params = " WHERE e.status = :s", {"s": status}
    async with tenant_session(user.org_id) as db:
        found = (await db.execute(text(SELECT + where + " ORDER BY e.created_at DESC LIMIT 500"), params)).all()
    return {"exceptions": [exception_out(r) for r in found]}


async def get_exception(db, exception_id: UUID, *, lock: bool = False):
    found = (
        await db.execute(text(SELECT + " WHERE e.id = :e" + (" FOR UPDATE OF e" if lock else "")), {"e": exception_id})
    ).first()
    if found is None:
        raise HTTPException(404, "Exception not found.")
    return found


@router.get("/{exception_id}")
async def get_one(exception_id: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        return exception_out(await get_exception(db, exception_id))


async def resolve_exception(
    db,
    user: Principal,
    exception_id: UUID,
    outcome: str,
    note: str | None,
    request: Request | None,
    *,
    source: str = "person",
) -> dict:
    found = await get_exception(db, exception_id, lock=True)
    if found.status not in OPEN:
        raise HTTPException(409, "This exception is already resolved.")
    if found.status == "escalated" and user.role != "admin":
        raise HTTPException(403, "An escalated exception is decided by an administrator.")
    inv = await db.get(Invoice, found.invoice_id, with_for_update=True)
    if inv.status != "exception":  # the invoice changed after this was raised: the exception is no longer about it
        raise HTTPException(
            409,
            f"The invoice is {inv.status.replace('_', ' ')}, not in exception, so this exception cannot be decided. Match it again first.",
        )
    if outcome == "escalate":
        await db.execute(
            text("UPDATE exceptions SET status = 'escalated', resolution = 'escalate', note = :n WHERE id = :e"),
            {"n": note, "e": exception_id},
        )
    else:
        await db.execute(
            text(
                "UPDATE exceptions SET status = 'resolved', resolution = :o, note = :n, resolved_by = :u, resolved_at = now() WHERE id = :e"
            ),
            {"o": outcome, "n": note, "u": user.id, "e": exception_id},
        )
        target = INVOICE_STATE[outcome]
        inv.status = (
            "matched" if target == "pending_approval" and inv.kind == "credit_note" else target
        )  # a credit note is applied, not paid
    audit(
        db,
        user,
        "exception.resolved",
        str(exception_id),
        {"outcome": outcome, "note": note, "invoice_id": str(inv.id), "source": source},
        request,
    )
    await db.flush()
    return exception_out(await get_exception(db, exception_id))


@router.post("/{exception_id}/resolve")
async def resolve(
    exception_id: UUID, body: Resolve, request: Request, user: Principal = Depends(require_role("approver", "admin"))
) -> dict:
    async with tenant_session(user.org_id) as db:
        return await resolve_exception(db, user, exception_id, body.outcome, body.note, request)


def question_for(exception_id) -> str:
    return f"[Exception {exception_id}] Investigate this exception and recommend an outcome."


@router.post("/{exception_id}/investigate", status_code=202)
async def investigate(
    exception_id: UUID, request: Request, user: Principal = Depends(require_role("clerk", "approver", "admin"))
) -> dict:
    from app.api.investigations import create_run

    async with tenant_session(user.org_id) as db:
        found = await get_exception(db, exception_id)
        if found.status not in OPEN:
            raise HTTPException(409, "This exception is already resolved.")
    run_id = await create_run(user, question_for(exception_id), request, exception_id=exception_id)
    async with tenant_session(user.org_id) as db:
        await db.execute(
            text("UPDATE exceptions SET status = 'investigating' WHERE id = :e AND status IN ('open', 'recommended')"),
            {"e": exception_id},
        )
    return {"run_id": str(run_id)}


@router.post("/{exception_id}/accept-recommendation")
async def accept(
    exception_id: UUID, request: Request, user: Principal = Depends(require_role("approver", "admin"))
) -> dict:
    from sqlalchemy import func, select

    from app.models.identity import User

    async with tenant_session(user.org_id) as db:
        found = await get_exception(db, exception_id, lock=True)
        if found.status not in OPEN:
            raise HTTPException(409, "This exception is already resolved.")
        if not found.recommendation:
            raise HTTPException(409, "There is no recommendation yet. Start an investigation first.")
        started_by = await db.scalar(
            text("SELECT user_id FROM runs WHERE id = :r"), {"r": found.recommendation.get("run_id")}
        )
        if started_by == user.id:
            deciders = await db.scalar(
                select(func.count()).select_from(User).where(User.role.in_(("approver", "admin")), User.is_active)
            )
            if deciders > 1:
                raise HTTPException(403, "You started this investigation; someone else must accept what it recommends.")
        return await resolve_exception(
            db,
            user,
            exception_id,
            found.recommendation["outcome"],
            "Accepted the agent's recommendation.",
            request,
            source="agent",
        )


@router.get("/{exception_id}/drafts")
async def drafts(exception_id: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        await get_exception(db, exception_id)
        found = (
            await db.execute(
                text(
                    "SELECT id, kind, body, run_id, created_at FROM drafts WHERE exception_id = :e ORDER BY created_at DESC"
                ),
                {"e": exception_id},
            )
        ).all()
    return {
        "drafts": [
            {
                "id": str(d.id),
                "kind": d.kind,
                "body": d.body,
                "run_id": str(d.run_id) if d.run_id else None,
                "created_at": d.created_at.isoformat(),
            }
            for d in found
        ]
    }
