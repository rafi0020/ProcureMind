from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import REAL, ForeignKey, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    vendor_id: Mapped[UUID] = mapped_column(ForeignKey("vendors.id"))
    po_number: Mapped[str]
    po_number_norm: Mapped[str]
    currency: Mapped[str]
    status: Mapped[str] = mapped_column(default="open")
    document_id: Mapped[UUID | None]
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class POLine(Base):
    __tablename__ = "po_lines"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    po_id: Mapped[UUID] = mapped_column(ForeignKey("purchase_orders.id"))
    line_no: Mapped[int]
    item_code: Mapped[str | None]
    description: Mapped[str]
    quantity: Mapped[Decimal]
    unit_price: Mapped[Decimal]


class POChange(Base):
    __tablename__ = "po_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    po_id: Mapped[UUID] = mapped_column(ForeignKey("purchase_orders.id"))
    changed_by: Mapped[UUID | None]
    at: Mapped[datetime] = mapped_column(server_default=func.now())
    before: Mapped[dict[str, Any]] = mapped_column(JSONB)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB)


class GoodsReceipt(Base):
    __tablename__ = "goods_receipts"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    po_id: Mapped[UUID] = mapped_column(ForeignKey("purchase_orders.id"))
    receipt_no: Mapped[str]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    document_id: Mapped[UUID | None]
    created_by: Mapped[UUID | None]


class ReceiptLine(Base):
    __tablename__ = "receipt_lines"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    receipt_id: Mapped[UUID] = mapped_column(ForeignKey("goods_receipts.id"))
    po_line_id: Mapped[UUID] = mapped_column(ForeignKey("po_lines.id"))
    quantity: Mapped[Decimal]


class Invoice(Base):
    """An invoice or a credit note. Amounts are stored as printed (positive); a credit note counts against."""

    __tablename__ = "invoices"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    document_id: Mapped[UUID | None]
    vendor_id: Mapped[UUID | None]
    vendor_name_printed: Mapped[str | None]
    kind: Mapped[str]
    invoice_number: Mapped[str | None]
    invoice_number_norm: Mapped[str | None]
    invoice_date: Mapped[date | None]
    due_date: Mapped[date | None]
    po_number: Mapped[str | None]
    currency: Mapped[str | None]
    subtotal: Mapped[Decimal | None]
    tax: Mapped[Decimal | None]
    total: Mapped[Decimal | None]
    bank_account: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="needs_review")
    issues: Mapped[list[str]] = mapped_column(JSONB, default=list)
    entered_by: Mapped[UUID | None]
    corrected_by: Mapped[list[UUID]] = mapped_column(ARRAY(PG_UUID(as_uuid=True)), default=list)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    pending_since: Mapped[datetime | None]
    escalated_at: Mapped[datetime | None]


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    invoice_id: Mapped[UUID] = mapped_column(ForeignKey("invoices.id"))
    line_no: Mapped[int]
    item_code: Mapped[str | None]
    description: Mapped[str] = mapped_column(default="")
    quantity: Mapped[Decimal | None]
    unit_price: Mapped[Decimal | None]
    amount: Mapped[Decimal | None]


class FieldProvenance(Base):
    __tablename__ = "field_provenance"
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    invoice_id: Mapped[UUID] = mapped_column(ForeignKey("invoices.id"), primary_key=True)
    field: Mapped[str] = mapped_column(primary_key=True)
    located: Mapped[bool]
    page: Mapped[int | None]
    bbox: Mapped[list[float] | None] = mapped_column(ARRAY(REAL))
    confidence: Mapped[float | None]
    candidate: Mapped[str | None]
    reason: Mapped[str | None]
