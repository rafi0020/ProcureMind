"""An organisation's matching tolerances, approval tiers and escalation delay (admin)."""

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from app.api.deps import audit, current_user, require_role
from app.db import tenant_session
from app.matching import tolerances
from app.security.sessions import Principal

router = APIRouter(prefix="/api/ap-settings", tags=["settings"])


class Tier(BaseModel):
    up_to: Decimal | None = Field(default=None, gt=0)
    approvals: int = Field(ge=1, le=5)
    role: str = Field(pattern="^(approver|admin)$")


class SettingsIn(BaseModel):
    price_tolerance_pct: Decimal | None = Field(default=None, ge=0, le=100)
    qty_tolerance_pct: Decimal | None = Field(default=None, ge=0, le=100)
    total_tolerance_abs: Decimal | None = Field(default=None, ge=0)
    approval_tiers: list[Tier] | None = Field(default=None, min_length=1, max_length=10)
    escalation_hours: int | None = Field(default=None, ge=1, le=720)

    @field_validator("approval_tiers")
    @classmethod
    def ordered(cls, tiers):
        if tiers is None:
            return tiers
        limits = [t.up_to for t in tiers]
        if limits[-1] is not None or any(x is None for x in limits[:-1]) or limits[:-1] != sorted(limits[:-1]):
            raise ValueError("Tiers must rise, and only the last may have no upper limit.")
        return tiers


def out(s: tolerances.Settings) -> dict:
    return {"price_tolerance_pct": f"{s.price_tolerance_pct:.2f}", "qty_tolerance_pct": f"{s.qty_tolerance_pct:.2f}",
            "total_tolerance_abs": f"{s.total_tolerance_abs:.2f}", "approval_tiers": s.approval_tiers, "escalation_hours": s.escalation_hours}  # fmt: skip


@router.get("")
async def get(user: Principal = Depends(current_user)) -> dict:
    async with tenant_session(user.org_id) as db:
        return out(await tolerances.load(db))


@router.put("")
async def put(body: SettingsIn, request: Request, admin: Principal = Depends(require_role("admin"))) -> dict:
    async with tenant_session(admin.org_id) as db:
        current = await tolerances.load(db)
        changes = body.model_dump(exclude_unset=True, mode="json")
        merged = out(current) | changes
        # Limits are numbers in storage, whatever JSON type the request used (a string such as "2500.00" included).
        tiers = [
            {**t, "up_to": None if t["up_to"] is None else float(Decimal(str(t["up_to"])))}
            for t in merged["approval_tiers"]
        ]
        await db.execute(
            text("INSERT INTO org_ap_settings (org_id, price_tolerance_pct, qty_tolerance_pct, total_tolerance_abs, approval_tiers, escalation_hours) "
                 "VALUES (:o, :p, :q, :t, CAST(:tiers AS jsonb), :e) ON CONFLICT (org_id) DO UPDATE SET price_tolerance_pct = excluded.price_tolerance_pct, "
                 "qty_tolerance_pct = excluded.qty_tolerance_pct, total_tolerance_abs = excluded.total_tolerance_abs, "
                 "approval_tiers = excluded.approval_tiers, escalation_hours = excluded.escalation_hours"),
            {"o": admin.org_id, "p": merged["price_tolerance_pct"], "q": merged["qty_tolerance_pct"], "t": merged["total_tolerance_abs"],
             "tiers": __import__("json").dumps(tiers), "e": merged["escalation_hours"]},
        )  # fmt: skip
        audit(db, admin, "ap_settings.changed", None, changes, request)
        return out(await tolerances.load(db))
