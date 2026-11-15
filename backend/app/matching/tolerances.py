"""An organisation's matching tolerances and approval tiers (defaults until an admin changes them)."""

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text

DEFAULT_TIERS = [{"up_to": 1000, "approvals": 1, "role": "approver"}, {"up_to": 10000, "approvals": 2, "role": "approver"},
                 {"up_to": None, "approvals": 2, "role": "admin"}]  # fmt: skip


@dataclass
class Settings:
    price_tolerance_pct: Decimal = Decimal("2.00")
    qty_tolerance_pct: Decimal = Decimal("0.00")
    total_tolerance_abs: Decimal = Decimal("1.00")
    approval_tiers: list = field(default_factory=lambda: list(DEFAULT_TIERS))
    escalation_hours: int = 48


async def load(db) -> Settings:
    row = (
        await db.execute(text("SELECT * FROM org_ap_settings"))
    ).first()  # row-level security: this organisation's only
    if row is None:
        return Settings()
    return Settings(
        row.price_tolerance_pct,
        row.qty_tolerance_pct,
        row.total_tolerance_abs,
        row.approval_tiers,
        row.escalation_hours,
    )
