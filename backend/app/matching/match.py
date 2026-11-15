"""The three-way match: purchase order, goods received, invoice. Deterministic: no model is involved.

Per invoice line: pair it with a PO line (by item code, else by description similarity of at least 0.5), then
compare quantities against what was received less what other accepted invoices already billed (credit notes
give back), and the unit price against the PO's within the organisation's tolerance.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, text

from app.ap.numbers import qty
from app.matching import tolerances
from app.models.ap import Invoice, InvoiceLine, POLine, PurchaseOrder, ReceiptLine

ACCEPTED = ("matched", "pending_approval", "approved")  # an invoice in these states has billed its quantities
SIMILAR = 0.5
HUNDRED = Decimal(100)


@dataclass
class Reason:
    code: str
    detail: str
    line_no: int | None = None


@dataclass
class MatchResult:
    status: str
    po_id: UUID | None
    reasons: list[Reason] = field(default_factory=list)
    lines: list[dict] = field(default_factory=list)


async def _find_po(db, inv: Invoice, po_id: UUID | None) -> PurchaseOrder | None:
    if po_id is not None:
        return await db.get(PurchaseOrder, po_id)
    from app.ap.numbers import normalise_number

    norm = normalise_number(inv.po_number)
    if not norm:
        return None
    return (await db.execute(select(PurchaseOrder).where(PurchaseOrder.po_number_norm == norm))).scalar_one_or_none()


async def _pair(db, line: InvoiceLine, po_lines: list[POLine]) -> POLine | None:
    if line.item_code:
        found = next((p for p in po_lines if p.item_code and p.item_code.casefold() == line.item_code.casefold()), None)
        if found:
            return found
    best, score = None, 0.0
    for p in po_lines:
        s = await db.scalar(
            text("SELECT similarity(lower(:a), lower(:b))"), {"a": line.description or "", "b": p.description}
        )
        if s > score:
            best, score = p, s
    return best if score >= SIMILAR else None


async def _billed(db, po_line_id: UUID, invoice_id: UUID) -> Decimal:
    """Quantity already billed on a PO line by other accepted invoices, less what credit notes gave back."""
    signed = text("""
        SELECT coalesce(sum(CASE WHEN i.kind = 'credit_note' THEN -ml.quantity ELSE ml.quantity END), 0)
        FROM match_lines ml JOIN matches m ON m.id = ml.match_id JOIN invoices i ON i.id = m.invoice_id
        WHERE ml.po_line_id = :l AND i.id <> :i AND i.status = ANY(:accepted)
    """)
    return Decimal(await db.scalar(signed, {"l": po_line_id, "i": invoice_id, "accepted": list(ACCEPTED)}))


async def _billed_value(db, po_id: UUID, invoice_id: UUID) -> Decimal:
    """Value (subtotal, or total when none was read) billed on the order by other accepted invoices, less credit notes."""
    return Decimal(await db.scalar(text("""
        SELECT coalesce(sum(CASE WHEN i.kind = 'credit_note' THEN -coalesce(i.subtotal, i.total) ELSE coalesce(i.subtotal, i.total) END), 0)
        FROM matches m JOIN invoices i ON i.id = m.invoice_id
        WHERE m.po_id = :p AND i.id <> :i AND i.status = ANY(:accepted)
    """), {"p": po_id, "i": invoice_id, "accepted": list(ACCEPTED)}))  # fmt: skip


async def _header_only_billed(db, po_id: UUID, invoice_id: UUID) -> bool:
    """Whether another accepted invoice on the order was matched without lines, so its quantities are not recorded."""
    return bool(await db.scalar(text("""
        SELECT EXISTS (SELECT 1 FROM matches m JOIN invoices i ON i.id = m.invoice_id
                       WHERE m.po_id = :p AND i.id <> :i AND i.status = ANY(:accepted)
                         AND NOT EXISTS (SELECT 1 FROM match_lines ml WHERE ml.match_id = m.id))
    """), {"p": po_id, "i": invoice_id, "accepted": list(ACCEPTED)}))  # fmt: skip


async def match_invoice(db, invoice_id: UUID, *, po_id: UUID | None = None) -> MatchResult:
    """Compare and decide. The caller stores the result (store_match) in the same transaction."""
    inv = await db.get(Invoice, invoice_id)
    settings = await tolerances.load(db)
    po = await _find_po(db, inv, po_id)
    if po is None:
        return MatchResult(
            "exception", None, [Reason("no_po", f"No purchase order {inv.po_number or '(none printed)'} was found.")]
        )
    # One match at a time per order, held until the caller stores the result: two invoices cannot both bill the same
    # receipt, since the second one reads what the first has billed only once the first has committed.
    await db.execute(text("SELECT 1 FROM purchase_orders WHERE id = :p FOR UPDATE"), {"p": po.id})
    await db.refresh(po)
    if po.status == "closed":
        return MatchResult("exception", po.id, [Reason("po_closed", f"Purchase order {po.po_number} is closed.")])
    if inv.vendor_id != po.vendor_id:
        return MatchResult(
            "exception", po.id, [Reason("vendor_mismatch", f"Purchase order {po.po_number} is with another vendor.")]
        )
    if inv.currency != po.currency:  # different currencies are never compared as numbers
        return MatchResult(
            "exception",
            po.id,
            [Reason("currency_mismatch", f"The invoice is in {inv.currency}, the purchase order in {po.currency}.")],
        )

    po_lines = list((await db.execute(select(POLine).where(POLine.po_id == po.id).order_by(POLine.line_no))).scalars())
    received = dict((await db.execute(select(ReceiptLine.po_line_id, func.sum(ReceiptLine.quantity)).where(
        ReceiptLine.po_line_id.in_([p.id for p in po_lines])).group_by(ReceiptLine.po_line_id))).all())  # fmt: skip
    lines = list(
        (
            await db.execute(
                select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id).order_by(InvoiceLine.line_no)
            )
        ).scalars()
    )
    result = MatchResult("matched", po.id)
    variance = False
    credit = inv.kind == "credit_note"
    complete = True  # every line paired and priced: then the lines' value can be held to the invoice total
    value = Decimal(0)
    taken: dict = {}  # quantity this invoice has already put against each PO line, from its earlier lines
    for line in lines:
        if line.quantity is not None and line.quantity < 0:
            # A negative line would quietly reduce what was billed. A credit is a credit note, so the line is refused.
            result.reasons.append(
                Reason(
                    "quantity_negative",
                    f"Line {line.line_no} ({line.description or line.item_code}) has a negative quantity ({qty(line.quantity)}). "
                    "A credit is a credit note, not a negative line.",
                    line.line_no,
                )
            )
            complete = False
            continue
        target = await _pair(db, line, po_lines)
        if target is None or line.quantity is None:
            result.reasons.append(
                Reason(
                    "line_unmatched",
                    f"Line {line.line_no} ({line.description or line.item_code}) is not on purchase order {po.po_number}.",
                    line.line_no,
                )
            )
            complete = False
            continue
        billed = await _billed(db, target.id, invoice_id)
        earlier = taken.get(target.id, Decimal(0))
        taken[target.id] = earlier + line.quantity
        got = received.get(target.id, Decimal(0))
        pct = (
            ((line.unit_price - target.unit_price) / target.unit_price * HUNDRED).quantize(Decimal("0.01"))
            if line.unit_price is not None and target.unit_price
            else None
        )
        result.lines.append({"invoice_line_id": str(line.id), "po_line_id": str(target.id), "line_no": line.line_no, "item_code": target.item_code,
                             "ordered": qty(target.quantity), "received": qty(got), "previously_invoiced": qty(billed), "invoiced": qty(line.quantity),
                             "po_unit_price": f"{target.unit_price.normalize():f}", "unit_price": None if line.unit_price is None else f"{line.unit_price.normalize():f}",
                             "price_variance_pct": None if pct is None else f"{pct:.2f}", "_quantity": line.quantity, "_price": line.unit_price, "_pct": pct})  # fmt: skip
        if line.unit_price is None:
            result.reasons.append(
                Reason(
                    "line_unpriced",
                    f"Line {line.line_no} ({line.description or line.item_code}) has no unit price, so its value cannot be checked.",
                    line.line_no,
                )
            )
            complete = False
        amount = (
            line.amount
            if line.amount is not None
            else (line.quantity * line.unit_price if line.unit_price is not None else None)
        )
        if amount is None:
            complete = False
        else:
            value += amount
        if credit:
            if earlier + line.quantity > billed:
                result.reasons.append(
                    Reason(
                        "credit_exceeds_invoiced",
                        f"Line {line.line_no}: credits {qty(line.quantity)}, only {qty(billed)} was invoiced.",
                        line.line_no,
                    )
                )
            continue
        allowed = got * (1 + settings.qty_tolerance_pct / HUNDRED)
        if billed + earlier + line.quantity > allowed:
            result.reasons.append(Reason("quantity_over_received",
                f"Line {line.line_no}: {qty(billed + earlier)} already invoiced plus {qty(line.quantity)} now is more than the {qty(got)} received ({qty(target.quantity)} ordered).", line.line_no))  # fmt: skip
        if pct is not None and pct > settings.price_tolerance_pct:
            result.reasons.append(Reason("price_over_tolerance",
                f"Line {line.line_no}: unit price {line.unit_price.normalize():f} is {pct}% above the purchase order's {target.unit_price.normalize():f} (tolerance {settings.price_tolerance_pct}%).", line.line_no))  # fmt: skip
        elif pct is not None and pct > 0:
            variance = True
    if lines and complete and inv.total is not None:
        # The matched lines, plus tax when it was read, must be the invoice total: otherwise the total is not what
        # was received and matched.
        matched_value = value + (inv.tax or Decimal(0))
        if abs(matched_value - inv.total) > settings.total_tolerance_abs:
            result.reasons.append(Reason("total_not_matched",
                f"The matched lines come to {matched_value:.2f}, but the invoice total is {inv.total:.2f}."))  # fmt: skip
    received_value = sum((received.get(p.id, Decimal(0)) * p.unit_price for p in po_lines), Decimal(0))
    if lines and not credit and await _header_only_billed(db, po.id, invoice_id):
        # Another invoice on this order was billed without lines, so its quantities are unknown: hold this invoice's
        # value to what was received less everything billed on the order, by value.
        billed_value = await _billed_value(db, po.id, invoice_id)
        if value + billed_value > received_value + settings.total_tolerance_abs:
            result.reasons.append(Reason("total_over_received",
                f"The lines come to {value:.2f}. {billed_value:.2f} is already invoiced on purchase order {po.po_number}, some of it without lines, and {received_value:.2f} was received."))  # fmt: skip
    basis = inv.subtotal if inv.subtotal is not None else inv.total
    if not lines and basis is not None and not credit:
        # No lines were read, so there is nothing to pair: hold the subtotal (or, when none was read, the total) to the
        # value received less what other accepted invoices on this order already billed. Never to the ordered value.
        billed_value = await _billed_value(db, po.id, invoice_id)
        if basis + billed_value > received_value + settings.total_tolerance_abs:
            result.reasons.append(Reason("total_over_received",
                f"No lines were read. The {'subtotal' if inv.subtotal is not None else 'total'} {basis:.2f} plus {billed_value:.2f} already invoiced is more than the {received_value:.2f} received on purchase order {po.po_number}."))  # fmt: skip
    result.status = "exception" if result.reasons else ("matched_with_variance" if variance else "matched")
    return result


async def store_match(
    db, invoice_id: UUID, result: MatchResult, *, manual: bool = False, user_id: UUID | None = None
) -> int:
    """Replace the invoice's match and move the invoice on: to approval, to an exception, or (credit note) applied.
    Approvals given before are void, since they approved a match that may no longer hold; returns how many were
    cleared."""
    from app.approvals.policy import clear_approvals

    inv = await db.get(Invoice, invoice_id)
    was_waiting = inv.status == "pending_approval"
    cleared = await clear_approvals(db, invoice_id)
    await db.execute(text("DELETE FROM matches WHERE invoice_id = :i"), {"i": invoice_id})
    match_id = await db.scalar(
        text("INSERT INTO matches (org_id, invoice_id, po_id, status, reasons, manual, matched_by) "
             "VALUES (:o, :i, :p, :s, CAST(:r AS jsonb), :m, :u) RETURNING id"),
        {"o": inv.org_id, "i": invoice_id, "p": result.po_id, "s": result.status, "m": manual, "u": user_id,
         "r": __import__("json").dumps([r.__dict__ for r in result.reasons])},
    )  # fmt: skip
    for line in result.lines:
        await db.execute(
            text("INSERT INTO match_lines (org_id, match_id, invoice_line_id, po_line_id, quantity, unit_price, price_variance_pct) "
                 "VALUES (:o, :m, :il, :pl, :q, :p, :v)"),
            {"o": inv.org_id, "m": match_id, "il": line["invoice_line_id"], "pl": line["po_line_id"], "q": line["_quantity"], "p": line["_price"], "v": line["_pct"]},
        )  # fmt: skip
    if result.status == "exception":
        inv.status = "exception"
    else:
        inv.status = (
            "matched" if inv.kind == "credit_note" else "pending_approval"
        )  # a credit note is applied, never paid
        if was_waiting and inv.status == "pending_approval":  # waiting again: the wait starts afresh
            await db.execute(text("UPDATE invoices SET pending_since = now() WHERE id = :i"), {"i": invoice_id})
    return cleared


def result_out(result: MatchResult, *, manual: bool = False) -> dict:
    return {"status": result.status, "po_id": str(result.po_id) if result.po_id else None, "manual": manual,
            "reasons": [r.__dict__ for r in result.reasons], "lines": [{k: v for k, v in ln.items() if not k.startswith("_")} for ln in result.lines]}  # fmt: skip


async def match_step(document_id: UUID) -> None:
    """Pipeline step: match an invoice made from this document, if it is ready."""
    from app.db import tenant_session
    from app.intake.steps import load_document

    ref = await load_document(document_id)
    async with tenant_session(ref.org_id) as db:
        inv = (await db.execute(select(Invoice).where(Invoice.document_id == document_id))).scalar_one_or_none()
        if inv is not None and inv.status == "ready":
            from app.checks.run import open_exception

            await store_match(db, inv.id, await match_invoice(db, inv.id))
            await open_exception(db, inv.id)  # the checks are part of this step: if they fail, the match is not kept
