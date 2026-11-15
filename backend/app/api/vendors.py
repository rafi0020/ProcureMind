"""Vendors, aliases and bank accounts. Bank changes need a second administrator."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.api.deps import audit, current_user, require_role
from app.db import tenant_session
from app.models.identity import User
from app.models.vendors import Vendor, VendorAlias, VendorBankAccount
from app.security.sessions import Principal

router = APIRouter(prefix="/api/vendors", tags=["vendors"])
Admin = Depends(require_role("admin"))
CURRENCY = r"^[A-Z]{3}$"


class VendorIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tax_id: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    default_currency: str = Field(pattern=CURRENCY)
    payment_terms_days: int = Field(default=30, ge=0, le=365)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class VendorPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    tax_id: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, max_length=200)
    default_currency: str | None = Field(default=None, pattern=CURRENCY)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    aliases: list[str] | None = Field(default=None, max_length=20)


class BankIn(BaseModel):
    iban_or_account: str = Field(min_length=4, max_length=64)
    bank_name: str = Field(min_length=1, max_length=120)


class Decision(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=500)


def vendor_out(v: Vendor) -> dict:
    return {"id": str(v.id), "name": v.name, "tax_id": v.tax_id, "email": v.email, "default_currency": v.default_currency,
            "payment_terms_days": v.payment_terms_days, "created_at": v.created_at.isoformat()}  # fmt: skip


def account_out(a: VendorBankAccount, *, full: bool = False) -> dict:
    """The account as an administrator sees it; for anyone else only its last four characters."""
    shown = a.iban_or_account if full else "…" + a.iban_or_account[-4:]
    return {"id": str(a.id), "iban_or_account": shown, "bank_name": a.bank_name, "status": a.status,
            "requested_by": str(a.requested_by) if a.requested_by else None,
            "decided_by": str(a.decided_by) if a.decided_by else None, "created_at": a.created_at.isoformat(),
            "decided_at": a.decided_at.isoformat() if a.decided_at else None}  # fmt: skip


def ending(account: str) -> str:
    return "…" + account[-4:]  # the audit log names an account without repeating it


async def get_vendor(db, vendor_id: UUID) -> Vendor:
    vendor = await db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(404, "Vendor not found.")
    return vendor


async def _set_aliases(db, user: Principal, vendor_id: UUID, aliases: list[str]) -> None:
    await db.execute(VendorAlias.__table__.delete().where(VendorAlias.vendor_id == vendor_id))
    for alias in dict.fromkeys(a.strip() for a in aliases if a.strip()):
        db.add(VendorAlias(org_id=user.org_id, vendor_id=vendor_id, alias=alias))


@router.get("")
async def list_vendors(user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        rows = (await db.execute(select(Vendor).order_by(func.lower(Vendor.name)))).scalars().all()
    return {"vendors": [vendor_out(v) for v in rows]}


@router.get("/{vendor_id}")
async def get_one(vendor_id: UUID, user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        vendor = await get_vendor(db, vendor_id)
        aliases = (
            (await db.execute(select(VendorAlias.alias).where(VendorAlias.vendor_id == vendor_id))).scalars().all()
        )
        accounts = (
            await db.execute(select(VendorBankAccount).where(VendorBankAccount.vendor_id == vendor_id).order_by(VendorBankAccount.created_at))
        ).scalars().all()  # fmt: skip
    full = user.role == "admin"
    return vendor_out(vendor) | {
        "aliases": sorted(aliases),
        "bank_accounts": [account_out(a, full=full) for a in accounts],
    }


@router.post("", status_code=201)
async def create(body: VendorIn, request: Request, admin: Principal = Admin) -> dict:
    try:
        async with tenant_session(admin.org_id) as db:
            vendor = Vendor(org_id=admin.org_id, **body.model_dump(exclude={"aliases"}))
            db.add(vendor)
            await db.flush()
            await db.refresh(vendor)
            await _set_aliases(db, admin, vendor.id, body.aliases)
            audit(db, admin, "vendor.created", str(vendor.id), {"name": vendor.name}, request)
    except IntegrityError as exc:
        raise HTTPException(409, "A vendor with this name or alias already exists.") from exc
    return vendor_out(vendor)


@router.patch("/{vendor_id}")
async def patch(vendor_id: UUID, body: VendorPatch, request: Request, admin: Principal = Admin) -> dict:
    try:
        async with tenant_session(admin.org_id) as db:
            vendor = await get_vendor(db, vendor_id)
            changes = body.model_dump(exclude_unset=True, exclude={"aliases"})
            for key, value in changes.items():
                setattr(vendor, key, value)
            if body.aliases is not None:
                await _set_aliases(db, admin, vendor_id, body.aliases)
            audit(
                db,
                admin,
                "vendor.updated",
                str(vendor_id),
                changes | ({"aliases": body.aliases} if body.aliases is not None else {}),
                request,
            )
            await db.flush()
            await db.refresh(vendor)
    except IntegrityError as exc:
        raise HTTPException(409, "A vendor with this name or alias already exists.") from exc
    return vendor_out(vendor)


@router.post("/{vendor_id}/bank-accounts", status_code=201)
async def request_bank_account(vendor_id: UUID, body: BankIn, request: Request, admin: Principal = Admin) -> dict:
    async with tenant_session(admin.org_id) as db:
        await get_vendor(db, vendor_id)
        account = VendorBankAccount(org_id=admin.org_id, vendor_id=vendor_id, iban_or_account=body.iban_or_account.replace(" ", ""),
                                    bank_name=body.bank_name, requested_by=admin.id)  # fmt: skip
        db.add(account)
        await db.flush()
        await db.refresh(account)
        audit(
            db,
            admin,
            "vendor.bank_account.requested",
            str(vendor_id),
            {"account": ending(account.iban_or_account)},
            request,
        )
    return account_out(account, full=True)


@router.post("/{vendor_id}/bank-accounts/{account_id}/decide")
async def decide_bank_account(
    vendor_id: UUID, account_id: UUID, body: Decision, request: Request, admin: Principal = Admin
) -> dict:
    async with tenant_session(admin.org_id) as db:
        account = (
            await db.execute(
                select(VendorBankAccount).where(VendorBankAccount.id == account_id, VendorBankAccount.vendor_id == vendor_id).with_for_update()
            )
        ).scalar_one_or_none()  # fmt: skip
        if account is None:
            raise HTTPException(404, "Bank account not found.")
        if account.status != "pending":
            raise HTTPException(409, f"This change was already {account.status}.")
        sole_admin = False
        if account.requested_by == admin.id:
            admins = await db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active))
            if admins > 1:
                raise HTTPException(403, "Another administrator must decide a bank-detail change.")
            sole_admin = True
        if body.decision == "approved":
            await db.execute(
                update(VendorBankAccount).where(VendorBankAccount.vendor_id == vendor_id, VendorBankAccount.status == "active").values(status="retired")
            )  # fmt: skip
            await db.flush()
        account.status = "active" if body.decision == "approved" else "rejected"
        account.decided_by, account.decided_at = admin.id, func.now()
        audit(db, admin, f"vendor.bank_account.{body.decision}", str(vendor_id),
              {"account": ending(account.iban_or_account), "sole_admin": sole_admin, "reason": body.reason}, request)  # fmt: skip
        await db.flush()
        await db.refresh(account)
    return account_out(account, full=True)
