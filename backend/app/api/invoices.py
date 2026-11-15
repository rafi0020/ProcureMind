"""Invoices and credit notes: list, detail with where each field was found, and corrections."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy import text as sql_text

from app.ap.numbers import money, normalise_number, price, qty
from app.ap.review import review_status
from app.api.deps import audit, current_user, require_role
from app.approvals.policy import clear_approvals
from app.db import tenant_session
from app.models.ap import FieldProvenance, Invoice, InvoiceLine
from app.models.vendors import Vendor
from app.security.sessions import Principal

router = APIRouter(prefix="/api/invoices", tags=["invoices"])
Writer = Depends(require_role("clerk", "approver", "admin"))
EDITABLE = ("needs_review", "ready", "exception", "matched", "on_hold")


class Correction(BaseModel):
    vendor_id: UUID | None = None
    invoice_number: str | None = Field(default=None, max_length=60)
    invoice_date: date | None = None
    due_date: date | None = None
    po_number: str | None = Field(default=None, max_length=60)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    subtotal: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    tax: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    total: Decimal | None = Field(default=None, max_digits=14, decimal_places=2)
    bank_account: str | None = Field(default=None, max_length=64)


def plain(v):
    if isinstance(v, Decimal):
        return money(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    return v


def invoice_out(i: Invoice) -> dict:
    keys = ("vendor_id", "vendor_name_printed", "kind", "invoice_number", "invoice_number_norm", "invoice_date", "due_date", "po_number",
            "currency", "subtotal", "tax", "total", "bank_account", "status", "issues")  # fmt: skip
    return {"id": str(i.id), "document_id": str(i.document_id) if i.document_id else None, **{k: plain(getattr(i, k)) for k in keys},
            "entered_by": plain(i.entered_by), "corrected_by": [str(u) for u in i.corrected_by or []], "created_at": i.created_at.isoformat()}  # fmt: skip


def line_out(ln: InvoiceLine) -> dict:
    return {"id": str(ln.id), "line_no": ln.line_no, "item_code": ln.item_code, "description": ln.description, "quantity": qty(ln.quantity),
            "unit_price": price(ln.unit_price), "amount": money(ln.amount)}  # fmt: skip


async def get_invoice(db, invoice_id: UUID, *, lock: bool = False) -> Invoice:
    stmt = select(Invoice).where(Invoice.id == invoice_id)
    inv = (await db.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if inv is None:
        raise HTTPException(404, "Invoice not found.")
    return inv


async def lines_of(db, invoice_id: UUID) -> list[InvoiceLine]:
    return list(
        (
            await db.execute(
                select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id).order_by(InvoiceLine.line_no)
            )
        ).scalars()
    )


@router.get("")
async def list_invoices(
    user: Principal = Depends(current_user),
    status: str | None = Query(None, max_length=20),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    stmt = select(Invoice).order_by(Invoice.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Invoice.status == status)
    async with tenant_session(user.org_id) as db:
        rows = (await db.execute(stmt)).scalars().all()
    return {"invoices": [invoice_out(i) for i in rows]}


@router.get("/{invoice_id}")
async def get_one(invoice_id: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        inv = await get_invoice(db, invoice_id)
        lines = await lines_of(db, invoice_id)
        prov = (
            (await db.execute(select(FieldProvenance).where(FieldProvenance.invoice_id == invoice_id))).scalars().all()
        )
    return invoice_out(inv) | {
        "lines": [line_out(ln) for ln in lines],
        "provenance": [
            {
                "field": p.field,
                "located": p.located,
                "page": p.page,
                "bbox": p.bbox,
                "confidence": p.confidence,
                "candidate": p.candidate,
                "reason": p.reason,
            }
            for p in prov
        ],  # fmt: skip
    }


@router.patch("/{invoice_id}/fields")
async def correct(invoice_id: UUID, body: Correction, request: Request, user: Principal = Writer) -> dict:
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "Nothing to change.")
    async with tenant_session(user.org_id) as db:
        inv = await get_invoice(db, invoice_id, lock=True)
        if inv.status not in EDITABLE:
            raise HTTPException(
                409,
                f"An invoice that is {inv.status.replace('_', ' ')} cannot be changed. An approver can send a waiting invoice back for correction; an administrator can reopen an approved one.",
            )
        if changes.get("vendor_id") is not None and await db.get(Vendor, changes["vendor_id"]) is None:
            raise HTTPException(404, "Vendor not found.")  # under this organisation's rows only: another's is not found
        before = {k: plain(getattr(inv, k)) for k in changes}
        for key, new in changes.items():
            setattr(inv, key, new)
        if "invoice_number" in changes:
            inv.invoice_number_norm = normalise_number(inv.invoice_number)
        if user.id not in (inv.corrected_by or []):
            inv.corrected_by = [*(inv.corrected_by or []), user.id]  # separation of duties reads this later
        # A corrected invoice is checked again from the start: any earlier match was made on the old values.
        await db.execute(sql_text("DELETE FROM matches WHERE invoice_id = :i"), {"i": invoice_id})
        await clear_approvals(db, invoice_id)  # approvals were of the values before this change
        await db.execute(  # the recommendation was made on the values before this change
            sql_text(
                "UPDATE exceptions SET recommendation = NULL, status = CASE WHEN status = 'recommended' THEN 'open' ELSE status END "
                "WHERE invoice_id = :i AND status = ANY(:open)"
            ),
            {"i": invoice_id, "open": ["open", "investigating", "recommended", "escalated"]},
        )
        inv.status, inv.issues = review_status(inv, await lines_of(db, invoice_id))
        audit(
            db,
            user,
            "invoice.corrected",
            str(invoice_id),
            {"before": before, "after": {k: plain(v) for k, v in changes.items()}},
            request,
        )
        await db.flush()
        await db.refresh(inv)
        return invoice_out(inv)


class MatchIn(BaseModel):
    po_id: UUID | None = None


MATCHABLE = ("ready", "matched", "exception", "pending_approval", "on_hold")


@router.post("/{invoice_id}/match")
async def match(invoice_id: UUID, body: MatchIn, request: Request, user: Principal = Writer) -> dict:
    """Match now; with po_id, match by hand to that purchase order (audited)."""
    from app.checks.run import open_exception
    from app.matching.match import match_invoice, result_out, store_match

    async with tenant_session(user.org_id) as db:
        inv = await get_invoice(db, invoice_id, lock=True)
        if inv.status not in MATCHABLE:
            raise HTTPException(
                409,
                "Correct the invoice first."
                if inv.status == "needs_review"
                else f"An invoice that is {inv.status} cannot be matched again.",
            )
        manual = body.po_id is not None
        result = await match_invoice(db, invoice_id, po_id=body.po_id)
        if manual and result.po_id is None:
            raise HTTPException(404, "Purchase order not found.")
        cleared = await store_match(db, invoice_id, result, manual=manual, user_id=user.id)
        if cleared:
            audit(
                db,
                user,
                "invoice.approvals_cleared",
                str(invoice_id),
                {"cleared": cleared, "reason": "matched again"},
                request,
            )
        await open_exception(db, invoice_id, user.id)
        if manual:
            audit(
                db,
                user,
                "invoice.matched_by_hand",
                str(invoice_id),
                {"po_id": str(body.po_id), "status": result.status},
                request,
            )
        return result_out(result, manual=manual)


@router.get("/{invoice_id}/match")
async def current_match(invoice_id: UUID, user: Principal = Depends(current_user)) -> dict:
    from sqlalchemy import text

    from app.matching.match import _billed

    async with tenant_session(user.org_id) as db:
        await get_invoice(db, invoice_id)
        m = (await db.execute(text("SELECT * FROM matches WHERE invoice_id = :i"), {"i": invoice_id})).first()
        if m is None:
            raise HTTPException(404, "This invoice has not been matched.")
        lines = (await db.execute(text("""
            SELECT ml.*, pl.line_no AS po_line_no, pl.item_code, pl.description, pl.quantity AS ordered, pl.unit_price AS po_unit_price,
                   il.line_no AS invoice_line_no
            FROM match_lines ml JOIN po_lines pl ON pl.id = ml.po_line_id JOIN invoice_lines il ON il.id = ml.invoice_line_id
            WHERE ml.match_id = :m ORDER BY il.line_no"""), {"m": m.id})).all()  # fmt: skip
        received = dict((await db.execute(text(
            "SELECT rl.po_line_id, sum(rl.quantity) FROM receipt_lines rl WHERE rl.po_line_id = ANY(:ids) GROUP BY 1"),
            {"ids": [r.po_line_id for r in lines]})).all())  # fmt: skip
        billed = {r.po_line_id: await _billed(db, r.po_line_id, invoice_id) for r in lines}
    return {"status": m.status, "po_id": str(m.po_id) if m.po_id else None, "manual": m.manual, "reasons": m.reasons,
            "lines": [{"invoice_line_no": r.invoice_line_no, "po_line_no": r.po_line_no, "item_code": r.item_code, "description": r.description,
                       "ordered": qty(r.ordered), "received": qty(Decimal(received.get(r.po_line_id, 0))), "previously_invoiced": qty(billed[r.po_line_id]), "invoiced": qty(r.quantity), "po_unit_price": price(r.po_unit_price), "unit_price": price(r.unit_price),
                       "price_variance_pct": None if r.price_variance_pct is None else f"{r.price_variance_pct:.2f}"} for r in lines]}  # fmt: skip


class Reopen(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


@router.post("/{invoice_id}/reopen")
async def reopen(
    invoice_id: UUID, body: Reopen, request: Request, admin: Principal = Depends(require_role("admin"))
) -> dict:
    """Take an approved invoice back for another decision; its approvals are cleared and the reason recorded."""
    async with tenant_session(admin.org_id) as db:
        inv = await get_invoice(db, invoice_id, lock=True)
        if inv.status != "approved":
            raise HTTPException(409, "Only an approved invoice can be reopened.")
        await db.execute(sql_text("DELETE FROM invoice_approvals WHERE invoice_id = :i"), {"i": invoice_id})
        inv.status = "pending_approval"
        audit(db, admin, "invoice.reopened", str(invoice_id), {"reason": body.reason}, request)
        await db.flush()
        await db.refresh(inv)
        return invoice_out(inv)


@router.post("/{invoice_id}/send-back")
async def send_back(
    invoice_id: UUID, body: Reopen, request: Request, user: Principal = Depends(require_role("approver", "admin"))
) -> dict:
    """Send a waiting invoice back for correction: its approvals so far are cleared, it can be changed again, and it
    is matched again before it returns to approval. The reason is recorded."""
    async with tenant_session(user.org_id) as db:
        inv = await get_invoice(db, invoice_id, lock=True)
        if inv.status != "pending_approval":
            raise HTTPException(409, "Only an invoice waiting for approval can be sent back.")
        await db.execute(sql_text("DELETE FROM invoice_approvals WHERE invoice_id = :i"), {"i": invoice_id})
        await db.execute(sql_text("DELETE FROM matches WHERE invoice_id = :i"), {"i": invoice_id})
        inv.status = "ready"
        audit(db, user, "invoice.sent_back", str(invoice_id), {"reason": body.reason}, request)
        await db.flush()
        await db.refresh(inv)
        return invoice_out(inv)
