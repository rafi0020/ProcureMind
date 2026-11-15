"""All checks for one invoice, and opening the exception when there is something for a person to decide."""

import json
from uuid import UUID

from sqlalchemy import text

from app.checks.anomalies import bank_changed, price_drift, split_under_threshold
from app.checks.duplicates import duplicates
from app.checks.findings import THRESHOLD, Finding
from app.config import get_settings
from app.models.ap import Invoice
from app.models.audit import AuditLog

OPEN = ("open", "investigating", "recommended", "escalated")


async def run_checks(db, invoice_id: UUID) -> list[Finding]:
    inv = await db.get(Invoice, invoice_id)
    found = []
    for check in (duplicates, bank_changed, price_drift, split_under_threshold):
        found += await check(db, inv)
    return found


async def close_cleared(db, invoice_id: UUID, user_id: UUID | None = None) -> None:
    """The invoice matches and has no findings any more: its open exception is closed, and the closing is audited."""
    closed = (
        await db.execute(
            text("UPDATE exceptions SET status = 'resolved', resolution = 'cleared_by_rematch', resolved_by = :u, resolved_at = now(), "
                 "note = 'The invoice matched cleanly when it was matched again.' "
                 "WHERE invoice_id = :i AND status = ANY(:open) RETURNING id, org_id"),
            {"i": invoice_id, "open": list(OPEN), "u": user_id},
        )
    ).all()  # fmt: skip
    for row in closed:
        db.add(AuditLog(org_id=row.org_id, user_id=user_id, action="exception.cleared_by_rematch", target=str(row.id),
                        detail={"invoice_id": str(invoice_id)}))  # fmt: skip


async def open_exception(db, invoice_id: UUID, user_id: UUID | None = None) -> UUID | None:
    """One open exception per invoice: the match's reasons plus findings scoring 0.6 or more."""
    inv = await db.get(Invoice, invoice_id)
    findings = [f for f in await run_checks(db, invoice_id) if f.score >= THRESHOLD]
    match = (
        await db.execute(text("SELECT status, reasons FROM matches WHERE invoice_id = :i"), {"i": invoice_id})
    ).first()
    reasons = match.reasons if match and match.status == "exception" else []
    if not findings and not reasons:
        await close_cleared(db, invoice_id, user_id)
        return None
    existing = await db.scalar(
        text("SELECT id FROM exceptions WHERE invoice_id = :i AND status = ANY(:open)"),
        {"i": invoice_id, "open": list(OPEN)},
    )
    payload = {"r": json.dumps(reasons), "f": json.dumps([f.__dict__ for f in findings])}
    if existing:
        await db.execute(
            text("UPDATE exceptions SET reasons = CAST(:r AS jsonb), findings = CAST(:f AS jsonb) WHERE id = :e"),
            payload | {"e": existing},
        )
    else:
        existing = await db.scalar(
            text(
                "INSERT INTO exceptions (org_id, invoice_id, reasons, findings) VALUES (:o, :i, CAST(:r AS jsonb), CAST(:f AS jsonb)) RETURNING id"
            ),
            payload | {"o": inv.org_id, "i": invoice_id},
        )
        if get_settings().auto_investigate:  # the agent starts on new exceptions when a model is configured for it
            from app.api.exceptions import question_for

            await db.execute(
                text(
                    "INSERT INTO runs (org_id, role, question, status, exception_id) VALUES (:o, 'clerk', :q, 'queued', :e)"
                ),
                {"o": inv.org_id, "q": question_for(existing), "e": existing},
            )
            await db.execute(text("UPDATE exceptions SET status = 'investigating' WHERE id = :e"), {"e": existing})
    if inv.status in ("ready", "matched", "pending_approval"):
        inv.status = "exception"  # held out of approval until a person decides
    return existing
