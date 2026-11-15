"""Which tier an amount falls in, and who may not approve an invoice (separation of duties)."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from app.models.ap import Invoice


def tier_for(total: Decimal, tiers: list[dict]) -> dict:
    for tier in tiers:
        if tier.get("up_to") is None or total <= Decimal(str(tier["up_to"])):
            return tier
    return tiers[-1]


async def clear_approvals(db, invoice_id: UUID) -> int:
    """Every approval given so far is void once what was approved may have changed. Returns how many were cleared."""
    return await db.scalar(
        text(
            "WITH gone AS (DELETE FROM invoice_approvals WHERE invoice_id = :i RETURNING 1) SELECT count(*) FROM gone"
        ),
        {"i": invoice_id},
    )


async def conflicts(db, invoices: list[Invoice], user_id: UUID) -> dict[UUID, str]:
    """Why this person may not approve each of these invoices (only those with a reason appear). Two queries,
    however many invoices: the approval queue asks this for every invoice waiting."""
    out: dict[UUID, str] = {}
    for inv in invoices:
        if inv.entered_by == user_id:
            out[inv.id] = "You entered this invoice; someone else must approve it."
        elif user_id in (inv.corrected_by or []):
            out[inv.id] = "You corrected this invoice; someone else must approve it."
    rest = [inv.id for inv in invoices if inv.id not in out]
    if rest:
        params = {"ids": rest, "u": user_id}
        matched = set(
            (
                await db.execute(
                    text("SELECT invoice_id FROM matches WHERE invoice_id = ANY(:ids) AND manual AND matched_by = :u"),
                    params,
                )
            ).scalars()
        )
        accepted = set((await db.execute(text("SELECT invoice_id FROM exceptions WHERE invoice_id = ANY(:ids) AND resolution = 'approve_with_variance' "
                                              "AND resolved_by = :u"), params)).scalars())  # fmt: skip
        # Whoever created the order, changed it, or recorded a receipt against it has a hand in what is being paid.
        ordered = set(
            (
                await db.execute(
                    text(
                        "SELECT m.invoice_id FROM matches m JOIN purchase_orders p ON p.id = m.po_id "
                        "WHERE m.invoice_id = ANY(:ids) AND (p.created_by = :u "
                        "OR EXISTS (SELECT 1 FROM po_changes c WHERE c.po_id = p.id AND c.changed_by = :u) "
                        "OR EXISTS (SELECT 1 FROM goods_receipts g WHERE g.po_id = p.id AND g.created_by = :u))"
                    ),
                    params,
                )
            ).scalars()
        )
        for i in rest:
            if i in matched:
                out[i] = "You matched this invoice by hand; someone else must approve it."
            elif i in accepted:
                out[i] = "You accepted this invoice's variance; someone else must approve it."
            elif i in ordered:
                out[i] = (
                    "You created, changed or received goods on this invoice's purchase order; someone else must approve it."
                )
    return out


async def conflict(db, inv: Invoice, user_id: UUID) -> str | None:
    """Why this person may not approve this invoice, or None."""
    return (await conflicts(db, [inv], user_id)).get(inv.id)
