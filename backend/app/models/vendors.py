from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    name: Mapped[str]
    tax_id: Mapped[str | None]
    email: Mapped[str | None]
    default_currency: Mapped[str]
    payment_terms_days: Mapped[int] = mapped_column(default=30)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VendorAlias(Base):
    """A name a vendor's documents use that is not its record name ("ACME LTD" for "Acme Supplies")."""

    __tablename__ = "vendor_aliases"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    vendor_id: Mapped[UUID] = mapped_column(ForeignKey("vendors.id"))
    alias: Mapped[str]


class VendorBankAccount(Base):
    __tablename__ = "vendor_bank_accounts"
    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    org_id: Mapped[UUID] = mapped_column(ForeignKey("orgs.id"))
    vendor_id: Mapped[UUID] = mapped_column(ForeignKey("vendors.id"))
    iban_or_account: Mapped[str]
    bank_name: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")  # pending | active | retired | rejected
    requested_by: Mapped[UUID | None]
    decided_by: Mapped[UUID | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    decided_at: Mapped[datetime | None]
