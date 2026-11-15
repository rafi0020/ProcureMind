"""Approving invoices for payment: my queue, and deciding."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from app.api.deps import audit, require_role
from app.approvals.chain import decide, states
from app.approvals.policy import conflicts
from app.db import tenant_session
from app.models.ap import Invoice
from app.models.vendors import Vendor
from app.security.sessions import Principal

router = APIRouter(prefix="/api/ap-approvals", tags=["approvals"])
Approver = Depends(require_role("approver", "admin"))


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=1000)


@router.get("")
async def queue(user: Principal = Depends(require_role("clerk", "approver", "admin")), mine: bool = False) -> dict:
    async with tenant_session(user.org_id) as db:
        rows = (
            await db.execute(select(Invoice, Vendor.name).outerjoin(Vendor, Vendor.id == Invoice.vendor_id).where(Invoice.status == "pending_approval")
                             .order_by(Invoice.escalated_at.is_(None), Invoice.pending_since))
        ).all()  # fmt: skip
        if mine:
            blocked = (
                await conflicts(db, [inv for inv, _ in rows], user.id) if user.role in ("approver", "admin") else None
            )
            rows = [(inv, vendor) for inv, vendor in rows if blocked is not None and inv.id not in blocked]
        described = await states(db, [inv for inv, _ in rows])
        ids = [inv.id for inv, _ in rows]
        matches = {m.invoice_id: m for m in (await db.execute(text(
            "SELECT m.invoice_id, m.status, coalesce(sum(ml.quantity * ml.unit_price), 0) AS value FROM matches m "
            "LEFT JOIN match_lines ml ON ml.match_id = m.id WHERE m.invoice_id = ANY(:ids) GROUP BY m.invoice_id, m.status"),
            {"ids": ids})).all()}  # fmt: skip
        resolved = {e.invoice_id: e for e in (await db.execute(text(
            "SELECT DISTINCT ON (e.invoice_id) e.invoice_id, e.resolution, u.name AS resolved_by FROM exceptions e "
            "LEFT JOIN users u ON u.id = e.resolved_by WHERE e.invoice_id = ANY(:ids) AND e.status = 'resolved' "
            "ORDER BY e.invoice_id, e.resolved_at DESC NULLS LAST"), {"ids": ids})).all()}  # fmt: skip
        out = []
        for inv, vendor in rows:
            m, e = matches.get(inv.id), resolved.get(inv.id)
            out.append(described[inv.id] | {
                "invoice_number": inv.invoice_number, "vendor": vendor, "total": f"{inv.total:.2f}", "currency": inv.currency,
                "pending_since": inv.pending_since.isoformat() if inv.pending_since else None, "escalated": inv.escalated_at is not None,
                "match": None if m is None else {"status": m.status, "value": f"{Decimal(m.value):.2f}"},
                "exception": None if e is None else {"resolution": e.resolution, "resolved_by": e.resolved_by or None},
            })  # fmt: skip
    return {"invoices": out}


@router.post("/{invoice_id}/decide")
async def decide_route(invoice_id: UUID, body: Decision, request: Request, user: Principal = Approver) -> dict:
    async with tenant_session(user.org_id) as db:
        result = await decide(db, invoice_id, user, body.decision, body.reason)
        audit(
            db,
            user,
            f"invoice.{body.decision}",
            str(invoice_id),
            {"reason": body.reason, "status": result["status"]},
            request,
        )
        return result
