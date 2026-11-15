"""Purchase orders and goods receipts, created by hand or from documents; every change to a PO is recorded."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.ap.numbers import normalise_number, price, qty
from app.api.deps import audit, current_user, require_role
from app.db import tenant_session
from app.models.ap import GoodsReceipt, POChange, POLine, PurchaseOrder, ReceiptLine
from app.models.vendors import Vendor
from app.security.sessions import Principal

router = APIRouter(prefix="/api/orders", tags=["orders"])
Writer = Depends(require_role("clerk", "approver", "admin"))
Qty = Field(gt=0, max_digits=14, decimal_places=3)


class LineIn(BaseModel):
    item_code: str | None = Field(default=None, max_length=60)
    description: str = Field(min_length=1, max_length=300)
    quantity: Decimal = Qty
    unit_price: Decimal = Field(ge=0, max_digits=14, decimal_places=4)


class OrderIn(BaseModel):
    vendor_id: UUID
    po_number: str = Field(min_length=1, max_length=60)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    lines: list[LineIn] = Field(min_length=1, max_length=500)


class LinePatch(BaseModel):
    id: UUID
    description: str | None = Field(default=None, min_length=1, max_length=300)
    quantity: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=3)
    unit_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=4)


class OrderPatch(BaseModel):
    status: Literal["open", "closed"] | None = None
    lines: list[LinePatch] | None = None


class ReceiptLineIn(BaseModel):
    po_line_id: UUID
    quantity: Decimal = Qty


class ReceiptIn(BaseModel):
    receipt_no: str = Field(min_length=1, max_length=60)
    lines: list[ReceiptLineIn] = Field(min_length=1, max_length=500)


async def received_by_line(db, po_id: UUID) -> dict[UUID, Decimal]:
    rows = await db.execute(
        select(ReceiptLine.po_line_id, func.sum(ReceiptLine.quantity)).join(POLine, POLine.id == ReceiptLine.po_line_id)
        .where(POLine.po_id == po_id).group_by(ReceiptLine.po_line_id)
    )  # fmt: skip
    return dict(rows.all())


async def snapshot(db, po: PurchaseOrder) -> dict:
    lines = (await db.execute(select(POLine).where(POLine.po_id == po.id).order_by(POLine.line_no))).scalars().all()
    received = await received_by_line(db, po.id)
    return {"id": str(po.id), "vendor_id": str(po.vendor_id), "po_number": po.po_number, "currency": po.currency, "status": po.status,
            "lines": [{"id": str(ln.id), "line_no": ln.line_no, "item_code": ln.item_code, "description": ln.description, "quantity": qty(ln.quantity),
                       "unit_price": price(ln.unit_price), "received": qty(received.get(ln.id, Decimal(0)))} for ln in lines]}  # fmt: skip


async def get_po(db, po_id: UUID, *, lock: bool = False) -> PurchaseOrder:
    stmt = select(PurchaseOrder).where(PurchaseOrder.id == po_id)
    po = (await db.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if po is None:
        raise HTTPException(404, "Purchase order not found.")
    return po


@router.post("", status_code=201)
async def create(body: OrderIn, request: Request, user: Principal = Writer) -> dict:
    try:
        async with tenant_session(user.org_id) as db:
            if await db.get(Vendor, body.vendor_id) is None:
                raise HTTPException(404, "Vendor not found.")
            po = PurchaseOrder(org_id=user.org_id, vendor_id=body.vendor_id, po_number=body.po_number, po_number_norm=normalise_number(body.po_number),
                               currency=body.currency, created_by=user.id)  # fmt: skip
            db.add(po)
            await db.flush()
            for n, ln in enumerate(body.lines, start=1):
                db.add(POLine(org_id=user.org_id, po_id=po.id, line_no=n, **ln.model_dump()))
            await db.flush()
            audit(db, user, "order.created", str(po.id), {"po_number": po.po_number}, request)
            return await snapshot(db, po)
    except IntegrityError as exc:
        raise HTTPException(409, "A purchase order with this number already exists.") from exc


@router.get("")
async def list_orders(user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        rows = (
            (await db.execute(select(PurchaseOrder).order_by(PurchaseOrder.created_at.desc()).limit(500)))
            .scalars()
            .all()
        )
    return {"orders": [{"id": str(p.id), "po_number": p.po_number, "vendor_id": str(p.vendor_id), "currency": p.currency, "status": p.status,
                        "created_at": p.created_at.isoformat()} for p in rows]}  # fmt: skip


@router.get("/{po_id}")
async def get_one(po_id: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        po = await get_po(db, po_id)
        changes = (
            (await db.execute(select(POChange).where(POChange.po_id == po_id).order_by(POChange.at.desc())))
            .scalars()
            .all()
        )
        receipts = (
            (
                await db.execute(
                    select(GoodsReceipt).where(GoodsReceipt.po_id == po_id).order_by(GoodsReceipt.received_at)
                )
            )
            .scalars()
            .all()
        )
        return await snapshot(db, po) | {
            "changes": [{"at": c.at.isoformat(), "changed_by": str(c.changed_by) if c.changed_by else None, "before": c.before, "after": c.after} for c in changes],
            "receipts": [{"id": str(r.id), "receipt_no": r.receipt_no, "received_at": r.received_at.isoformat()} for r in receipts],
        }  # fmt: skip


@router.patch("/{po_id}")
async def patch(po_id: UUID, body: OrderPatch, request: Request, user: Principal = Writer) -> dict:
    async with tenant_session(user.org_id) as db:
        po = await get_po(db, po_id, lock=True)
        before = await snapshot(db, po)
        if body.status:
            po.status = body.status
        for change in body.lines or []:
            line = (
                await db.execute(select(POLine).where(POLine.id == change.id, POLine.po_id == po_id))
            ).scalar_one_or_none()
            if line is None:
                raise HTTPException(404, "Line not found on this purchase order.")
            for key, value in change.model_dump(exclude_unset=True, exclude={"id"}).items():
                setattr(line, key, value)
        await db.flush()
        after = await snapshot(db, po)
        db.add(POChange(org_id=user.org_id, po_id=po_id, changed_by=user.id, before=before, after=after))
        audit(db, user, "order.changed", str(po_id), None, request)
        return after


@router.post("/{po_id}/receipts", status_code=201)
async def receive(po_id: UUID, body: ReceiptIn, request: Request, user: Principal = Writer) -> dict:
    async with tenant_session(user.org_id) as db:
        po = await get_po(db, po_id, lock=True)  # one receipt at a time per PO, so the totals below cannot race
        if await db.scalar(
            select(GoodsReceipt.id).where(GoodsReceipt.po_id == po_id, GoodsReceipt.receipt_no == body.receipt_no)
        ):
            raise HTTPException(409, "A receipt with this number is already recorded on this order.")
        lines = {ln.id: ln for ln in (await db.execute(select(POLine).where(POLine.po_id == po_id))).scalars()}
        received = await received_by_line(db, po_id)
        receipt = GoodsReceipt(org_id=user.org_id, po_id=po_id, receipt_no=body.receipt_no, created_by=user.id)
        db.add(receipt)
        await db.flush()
        for item in body.lines:
            line = lines.get(item.po_line_id)
            if line is None:
                raise HTTPException(404, "Line not found on this purchase order.")
            if received.get(line.id, Decimal(0)) + item.quantity > line.quantity:
                raise HTTPException(
                    422,
                    f"Line {line.line_no}: receiving {item.quantity} would exceed the {qty(line.quantity)} ordered.",
                )
            received[line.id] = received.get(line.id, Decimal(0)) + item.quantity
            db.add(ReceiptLine(org_id=user.org_id, receipt_id=receipt.id, po_line_id=line.id, quantity=item.quantity))
        audit(db, user, "order.received", str(po_id), {"receipt_no": body.receipt_no}, request)
        await db.flush()
        return await snapshot(db, po) | {"receipt_id": str(receipt.id)}
