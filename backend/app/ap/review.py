"""When an invoice is ready to be matched: required fields present and the printed arithmetic agrees."""

from decimal import Decimal

from app.models.ap import Invoice, InvoiceLine

CENT = Decimal("0.01")
REQUIRED = ("vendor_id", "invoice_number", "invoice_date", "currency", "total")
LABEL = {
    "vendor_id": "vendor",
    "invoice_number": "invoice number",
    "invoice_date": "invoice date",
    "currency": "currency",
    "total": "total",
}


def issues_of(inv: Invoice, lines: list[InvoiceLine]) -> list[str]:
    issues = [f"Missing {LABEL[f]}." for f in REQUIRED if getattr(inv, f) in (None, "")]
    for ln in lines:
        if ln.quantity is not None and ln.unit_price is not None and ln.amount is not None:
            expected = (ln.quantity * ln.unit_price).quantize(CENT)
            if abs(expected - ln.amount) > CENT:
                issues.append(
                    f"Line {ln.item_code or ln.line_no}: {ln.quantity} x {ln.unit_price} is {expected}, printed {ln.amount}."
                )
    amounts = [ln.amount for ln in lines if ln.amount is not None]
    if amounts and inv.subtotal is not None and abs(sum(amounts) - inv.subtotal) > CENT * len(amounts):
        issues.append(f"Lines add up to {sum(amounts)}, the subtotal says {inv.subtotal}.")
    if None not in (inv.subtotal, inv.tax, inv.total) and abs(inv.subtotal + inv.tax - inv.total) > CENT:
        issues.append(f"Subtotal {inv.subtotal} plus tax {inv.tax} is not the total {inv.total}.")
    return issues


def review_status(inv: Invoice, lines: list[InvoiceLine]) -> tuple[str, list[str]]:
    issues = issues_of(inv, lines)
    return ("needs_review" if issues else "ready"), issues
