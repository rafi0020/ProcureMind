"""The same invoice twice: by number (normalised) or by amount and date. Identical files never get this far:
intake stores a file once per organisation."""

from datetime import timedelta

from sqlalchemy import select

from app.checks.findings import Finding
from app.models.ap import Invoice


async def duplicates(db, inv: Invoice) -> list[Finding]:
    if inv.vendor_id is None:
        return []
    out = []
    others = select(Invoice).where(
        Invoice.vendor_id == inv.vendor_id, Invoice.kind == inv.kind, Invoice.id != inv.id, Invoice.status != "rejected"
    )
    if inv.invoice_number_norm:
        same = (await db.execute(others.where(Invoice.invoice_number_norm == inv.invoice_number_norm))).scalars().all()
        if same:
            out.append(Finding("duplicate_number", 0.95, f"Invoice number {inv.invoice_number} from this vendor was already received as {same[0].invoice_number}.",
                               [str(s.id) for s in same]))  # fmt: skip
    if inv.total is not None and inv.invoice_date is not None and inv.kind == "invoice":
        near = (
            await db.execute(others.where(
                Invoice.total == inv.total, Invoice.invoice_date.between(inv.invoice_date - timedelta(days=30), inv.invoice_date + timedelta(days=30)),
                Invoice.invoice_number_norm.is_distinct_from(inv.invoice_number_norm)))  # those are duplicate_number already
        ).scalars().all()  # fmt: skip
        if near:
            out.append(Finding("duplicate_amount", 0.7, f"The same amount, {inv.total} {inv.currency}, was invoiced by this vendor within 30 days ({near[0].invoice_number}).",
                               [str(s.id) for s in near]))  # fmt: skip
    return out
