"""Deciding an invoice. Everything happens with the invoice row locked, so two approvers cannot both finish it."""

from datetime import timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text

from app.approvals.policy import conflict, tier_for
from app.db import system_session, tenant_session
from app.matching import tolerances
from app.models.ap import Invoice
from app.security.sessions import Principal
from app.worker import scheduler


def describe(inv: Invoice, tiers: list[dict], roles: list[str]) -> dict:
    """Where an invoice stands in its approval chain, from the organisation's tiers and the roles of those who
    approved it so far."""
    tier = tier_for(inv.total, tiers)
    count, admin = len(roles), "admin" in roles
    needs_admin = tier.get("role") == "admin" and not admin
    waiting = None
    if inv.status == "pending_approval":
        if count < tier["approvals"]:
            waiting = f"{tier['approvals'] - count} more approval(s)" + (
                ", one from an administrator" if needs_admin else ""
            )
        elif needs_admin:
            waiting = "an administrator"
    return {"id": str(inv.id), "status": inv.status, "approvals": count, "required": tier["approvals"], "needs_admin": tier.get("role") == "admin",
            "waiting_for": waiting}  # fmt: skip


async def states(db, invoices: list[Invoice]) -> dict:
    """describe() for many invoices in two queries."""
    tiers = (await tolerances.load(db)).approval_tiers
    roles: dict = {inv.id: [] for inv in invoices}
    if invoices:
        found = await db.execute(text("SELECT invoice_id, approver_role FROM invoice_approvals WHERE invoice_id = ANY(:ids) AND decision = 'approved'"),
                                 {"ids": list(roles)})  # fmt: skip
        for invoice_id, role in found:
            roles[invoice_id].append(role)
    return {inv.id: describe(inv, tiers, roles[inv.id]) for inv in invoices}


async def state(db, inv: Invoice) -> dict:
    return (await states(db, [inv]))[inv.id]


async def decide(db, invoice_id: UUID, user: Principal, decision: str, reason: str | None) -> dict:
    inv = (await db.execute(select(Invoice).where(Invoice.id == invoice_id).with_for_update())).scalar_one_or_none()
    if inv is None:
        raise HTTPException(404, "Invoice not found.")
    if inv.status != "pending_approval":
        raise HTTPException(409, f"This invoice is {inv.status.replace('_', ' ')}, not waiting for approval.")
    if why := await conflict(db, inv, user.id):
        raise HTTPException(403, why)
    if await db.scalar(
        text("SELECT 1 FROM invoice_approvals WHERE invoice_id = :i AND approver_id = :u"),
        {"i": invoice_id, "u": user.id},
    ):
        raise HTTPException(409, "You have already decided this invoice.")
    await db.execute(
        text(
            "INSERT INTO invoice_approvals (org_id, invoice_id, approver_id, approver_role, decision, reason) VALUES (:o, :i, :u, :r, :d, :why)"
        ),
        {"o": inv.org_id, "i": invoice_id, "u": user.id, "r": user.role, "d": decision, "why": reason},
    )
    if decision == "rejected":
        inv.status = "rejected"
    else:
        now = await state(db, inv)
        if now["waiting_for"] is None:
            inv.status = "approved"  # approved for payment: recorded, never paid by this system
    await db.flush()
    return await state(db, inv)


@scheduler.every(900, "escalate_overdue_invoices")
async def escalate_overdue() -> None:
    """Mark invoices that have waited longer than the organisation's escalation delay; queues show them first."""
    async with system_session() as db:
        orgs = [r.id for r in (await db.execute(text("SELECT id FROM orgs"))).all()]
    for org_id in orgs:
        async with tenant_session(org_id) as db:
            hours = (await tolerances.load(db)).escalation_hours
            await db.execute(
                text(
                    "UPDATE invoices SET escalated_at = now() WHERE status = 'pending_approval' AND escalated_at IS NULL AND pending_since < :cutoff"
                ),
                {"cutoff": __import__("datetime").datetime.now(__import__("datetime").UTC) - timedelta(hours=hours)},
            )
