"""Anomalies: a bank account that is not the vendor's, a price far above history, splitting under a limit."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.checks.findings import ACCEPTED, Finding
from app.matching import tolerances
from app.models.ap import Invoice, InvoiceLine
from app.models.vendors import VendorBankAccount

DRIFT = Decimal("1.20")


def _account(value: str | None) -> str:
    return "".join((value or "").split()).upper()


LOOKALIKES = str.maketrans({"O": "0", "I": "1", "L": "1"})


def _same_account(a: str, b: str) -> bool:
    """Equal, or equal once the letters OCR confuses with digits (O/0, I/1, L/1) are read as digits after the
    two-letter country code: a scan's misreading is not a new account. Any real difference in digits still counts."""
    return a == b or (a[:2] == b[:2] and a[2:].translate(LOOKALIKES) == b[2:].translate(LOOKALIKES))


async def bank_changed(db, inv: Invoice) -> list[Finding]:
    if not inv.bank_account or inv.vendor_id is None:
        return []
    active = await db.scalar(
        select(VendorBankAccount.iban_or_account).where(
            VendorBankAccount.vendor_id == inv.vendor_id, VendorBankAccount.status == "active"
        )
    )
    if active is None:  # nothing approved to check the printed account against: a person must verify it
        return [
            Finding(
                "bank_unverified",
                0.6,
                f"The invoice asks for payment to account ending {_account(inv.bank_account)[-4:]}; this vendor has no active account on file to check it against.",
            )
        ]
    if _same_account(_account(active), _account(inv.bank_account)):
        return []
    return [
        Finding(
            "bank_changed",
            0.9,
            f"The invoice asks for payment to account ending {_account(inv.bank_account)[-4:].translate(LOOKALIKES)}; the vendor's account on file ends {_account(active)[-4:]}.",
        )
    ]


async def price_drift(db, inv: Invoice) -> list[Finding]:
    if inv.vendor_id is None or inv.invoice_date is None:
        return []
    out = []
    lines = (await db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id))).scalars().all()
    for line in lines:
        if not line.item_code or line.unit_price is None:
            continue
        highest = await db.scalar(
            select(func.max(InvoiceLine.unit_price)).join(Invoice, Invoice.id == InvoiceLine.invoice_id).where(
                Invoice.vendor_id == inv.vendor_id, Invoice.id != inv.id, Invoice.status.in_(ACCEPTED), InvoiceLine.item_code == line.item_code,
                Invoice.invoice_date.between(inv.invoice_date - timedelta(days=180), inv.invoice_date))
        )  # fmt: skip
        if highest and line.unit_price > highest * DRIFT:
            out.append(
                Finding(
                    "price_drift",
                    0.6,
                    f"Item {line.item_code}: {line.unit_price.normalize():f} is more than 20% above this vendor's highest price in 180 days ({highest.normalize():f}).",
                )
            )
    return out


async def split_under_threshold(db, inv: Invoice) -> list[Finding]:
    if inv.vendor_id is None or inv.total is None or inv.invoice_date is None or inv.kind != "invoice":
        return []
    settings = await tolerances.load(db)
    for tier in settings.approval_tiers:
        limit = tier.get("up_to")
        if limit is None:
            continue
        limit = Decimal(str(limit))
        if not (limit * Decimal("0.9") <= inv.total < limit):
            continue
        close = (
            await db.execute(select(Invoice.invoice_number, Invoice.total).where(
                Invoice.vendor_id == inv.vendor_id, Invoice.kind == "invoice", Invoice.status != "rejected",
                Invoice.total >= limit * Decimal("0.9"), Invoice.total < limit,
                Invoice.invoice_date.between(inv.invoice_date - timedelta(days=7), inv.invoice_date)))
        ).all()  # fmt: skip
        if len(close) >= 2 and sum(t for _, t in close) > limit:
            shown = f"{limit.normalize():f}"
            return [
                Finding(
                    "split_under_threshold",
                    0.8,
                    f"{len(close)} invoices from this vendor within 7 days, each just under the {shown} approval limit, add up to {sum(t for _, t in close)}.",
                )
            ]
    return []
