"""Pipeline step: turn a document's extraction into an invoice, a purchase order or a goods receipt."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, text

from app.ap.numbers import normalise_number
from app.ap.review import review_status
from app.db import tenant_session
from app.extraction import parse
from app.models.ap import FieldProvenance, GoodsReceipt, Invoice, InvoiceLine, POLine, PurchaseOrder, ReceiptLine
from app.models.vendors import Vendor
from app.vendors import find_vendor


def value(fields: dict, name: str):
    f = fields.get(name) or {}
    return f.get("value") if f.get("located") else None


def as_decimal(v) -> Decimal | None:
    return None if v is None else parse.amount(v)


def as_date(v) -> date | None:
    return date.fromisoformat(v) if v else None


async def _vendor(db, fields: dict) -> tuple[Vendor | None, str | None]:
    """The vendor the document names, and an issue when a person must choose. Two vendors may share a tax ID (a group
    with several accounts, or a data-entry slip): the document is then held, never assigned to one of them by chance."""
    tax_id = value(fields, "vendor_tax_id")
    if tax_id:
        found = (
            (
                await db.execute(
                    select(Vendor)
                    .where(func.upper(Vendor.tax_id) == tax_id.upper())
                    .order_by(Vendor.created_at, Vendor.id)
                )
            )
            .scalars()
            .all()
        )
        if len(found) == 1:
            return found[0], None
        if len(found) > 1:
            names = ", ".join(v.name for v in found)
            return None, f"The tax ID {tax_id} belongs to {len(found)} vendors ({names}); a person chooses the vendor."
    name = value(fields, "vendor_name")
    return (await find_vendor(db, name) if name else None), None


async def create_invoice(db, doc, extraction) -> Invoice:
    fields, kind = extraction.fields, extraction.kind
    vendor, vendor_issue = await _vendor(db, fields)
    inv = Invoice(org_id=doc.org_id, document_id=doc.id, vendor_id=vendor.id if vendor else None, vendor_name_printed=value(fields, "vendor_name"),
                  kind=kind, invoice_number=value(fields, "doc_number"), invoice_number_norm=normalise_number(value(fields, "doc_number")),
                  invoice_date=as_date(value(fields, "doc_date")), due_date=as_date(value(fields, "due_date")), po_number=value(fields, "po_number"),
                  currency=value(fields, "currency"), subtotal=as_decimal(value(fields, "subtotal")), tax=as_decimal(value(fields, "tax")),
                  total=as_decimal(value(fields, "total")), bank_account=value(fields, "bank_account"), entered_by=doc.uploaded_by)  # fmt: skip
    db.add(inv)
    await db.flush()
    lines = []
    for n, ln in enumerate(extraction.lines, start=1):
        line = InvoiceLine(org_id=doc.org_id, invoice_id=inv.id, line_no=n, item_code=ln.get("item_code"), description=ln.get("description") or "",
                           quantity=as_decimal(ln.get("quantity")), unit_price=as_decimal(ln.get("unit_price")), amount=as_decimal(ln.get("amount")))  # fmt: skip
        db.add(line)
        lines.append(line)
    for name, f in fields.items():
        db.add(FieldProvenance(org_id=doc.org_id, invoice_id=inv.id, field=name, located=bool(f.get("located")), page=f.get("page"),
                               bbox=list(f["bbox"]) if f.get("bbox") else None, confidence=f.get("confidence"),
                               candidate=None if f.get("candidate") is None else str(f["candidate"]), reason=f.get("reason")))  # fmt: skip
    inv.status, inv.issues = review_status(inv, lines)
    held = [issue for issue in extraction.issues if issue not in inv.issues]  # e.g. a model line that was left out
    held += [vendor_issue] if vendor_issue else []
    disputed = [  # the rules and the model read a located value differently: a person says which is right
        f"{name} was read differently by the rules and the model; a person confirms it."
        for name in (extraction.flags or {}).get("disagreement", [])
    ]
    if held or disputed:
        inv.status, inv.issues = "needs_review", [*inv.issues, *held, *disputed]
    return inv


class Refused(Exception):
    """A document the records cannot take as it is. A person decides; the message says why, and the document is
    left for review with it."""


INBOX_HELD = "Orders and receipts arriving by the inbox are confirmed by a person."


async def create_po(db, doc, extraction) -> PurchaseOrder:
    fields = extraction.fields
    vendor, vendor_issue = await _vendor(db, fields)
    number = value(fields, "doc_number")
    lines = [
        ln for ln in extraction.lines if as_decimal(ln.get("quantity")) and as_decimal(ln.get("unit_price")) is not None
    ]
    if vendor is None or not number or not lines:  # an order without lines would match nothing; a person looks
        raise Refused(vendor_issue or "The vendor, the PO number or the order lines could not be read.")
    if await db.scalar(select(PurchaseOrder.id).where(PurchaseOrder.po_number_norm == normalise_number(number))):
        raise Refused(f"A purchase order numbered {number} already exists.")
    po = PurchaseOrder(org_id=doc.org_id, vendor_id=vendor.id, po_number=number, po_number_norm=normalise_number(number),
                       currency=value(fields, "currency") or vendor.default_currency, document_id=doc.id, created_by=doc.uploaded_by)  # fmt: skip
    db.add(po)
    await db.flush()
    for n, ln in enumerate(lines, start=1):
        db.add(POLine(org_id=doc.org_id, po_id=po.id, line_no=n, item_code=ln.get("item_code"), description=ln.get("description") or "",
                      quantity=as_decimal(ln["quantity"]), unit_price=as_decimal(ln["unit_price"])))  # fmt: skip
    return po


async def create_receipt(db, doc, extraction) -> str | None:
    """A goods receipt from a document. Lines over what was ordered (counting what is already received) are left
    out; the returned text says which, and the document is held for a person when there is any."""
    fields = extraction.fields
    po = (
        await db.execute(
            select(PurchaseOrder).where(PurchaseOrder.po_number_norm == normalise_number(value(fields, "po_number")))
        )
    ).scalar_one_or_none()
    if po is None:
        raise Refused("No purchase order with this number.")
    # Held for the whole transaction: no other receipt or match on this order can read the received quantities
    # between this check and the insert.
    await db.execute(text("SELECT 1 FROM purchase_orders WHERE id = :p FOR UPDATE"), {"p": po.id})
    receipt_no = value(fields, "doc_number") or f"doc-{str(doc.id)[:8]}"
    if await db.scalar(
        select(GoodsReceipt.id).where(GoodsReceipt.po_id == po.id, GoodsReceipt.receipt_no == receipt_no)
    ):
        raise Refused(f"Receipt {receipt_no} is already recorded on purchase order {po.po_number}.")
    po_lines = {pl.item_code: pl for pl in (await db.execute(select(POLine).where(POLine.po_id == po.id))).scalars()}
    received = dict((await db.execute(select(ReceiptLine.po_line_id, func.sum(ReceiptLine.quantity)).where(
        ReceiptLine.po_line_id.in_([pl.id for pl in po_lines.values()])).group_by(ReceiptLine.po_line_id))).all())  # fmt: skip
    accepted, refused = [], []
    for ln in extraction.lines:
        target, quantity = po_lines.get(ln.get("item_code")), as_decimal(ln.get("quantity"))
        if not (target and quantity):
            continue
        now = received.get(target.id, Decimal(0))
        if now + quantity > target.quantity:
            refused.append(f"{target.item_code}: receiving {quantity} would take {now + quantity} over the {target.quantity} ordered; the line was left out.")  # fmt: skip
            continue
        received[target.id] = now + quantity
        accepted.append((target, quantity))
    if not accepted:
        raise Refused(" ".join(refused) or "No line of this receipt matches the purchase order.")
    receipt = GoodsReceipt(
        org_id=doc.org_id, po_id=po.id, receipt_no=receipt_no, document_id=doc.id, created_by=doc.uploaded_by
    )
    db.add(receipt)
    await db.flush()
    for target, quantity in accepted:
        db.add(ReceiptLine(org_id=doc.org_id, receipt_id=receipt.id, po_line_id=target.id, quantity=quantity))
    return " ".join(refused) or None


async def _hold(db, document_id: UUID, message: str) -> None:
    await db.execute(
        text("UPDATE documents SET status = 'needs_review', error = :e WHERE id = :d"),
        {"e": message[:1000], "d": document_id},
    )


async def records_step(document_id: UUID) -> None:
    from app.intake.steps import load_document

    ref = await load_document(document_id)
    async with tenant_session(ref.org_id) as db:
        doc = (
            await db.execute(
                text("SELECT id, org_id, uploaded_by, source FROM documents WHERE id = :d"), {"d": document_id}
            )
        ).one()
        extraction = (
            await db.execute(
                text("SELECT kind, fields, lines, issues, flags FROM extractions WHERE document_id = :d"),
                {"d": document_id},
            )
        ).first()
        if extraction is None:
            return
        if extraction.kind in ("invoice", "credit_note"):
            if (
                await db.scalar(select(Invoice.id).where(Invoice.document_id == document_id)) is None
            ):  # a retry adds nothing
                await create_invoice(db, doc, extraction)
            return
        if extraction.kind not in ("purchase_order", "goods_receipt"):
            return
        if extraction.kind == "purchase_order":
            made = await db.scalar(select(PurchaseOrder.id).where(PurchaseOrder.document_id == document_id))
        else:
            made = await db.scalar(select(GoodsReceipt.id).where(GoodsReceipt.document_id == document_id))
        if made is not None:  # a retry adds nothing
            return
        if doc.source == "inbox":  # a supplier's mail is not a person confirming an order or a receipt
            await _hold(db, document_id, INBOX_HELD)
            return
        try:
            if extraction.kind == "purchase_order":
                await create_po(db, doc, extraction)
            else:
                note = await create_receipt(db, doc, extraction)
                if note:
                    await _hold(db, document_id, note)
        except Refused as exc:
            await _hold(db, document_id, str(exc))
