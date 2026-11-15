"""Matching: each organisation's tolerances and approval tiers, and the current match of each invoice."""

from alembic import op

from app.rls import enable_rls

revision = "0015"
down_revision = "0014"
TIERS = '[{"up_to": 1000, "approvals": 1, "role": "approver"}, {"up_to": 10000, "approvals": 2, "role": "approver"}, {"up_to": null, "approvals": 2, "role": "admin"}]'


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE org_ap_settings (
            org_id uuid PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
            price_tolerance_pct numeric(6,2) NOT NULL DEFAULT 2.00 CHECK (price_tolerance_pct BETWEEN 0 AND 100),
            qty_tolerance_pct numeric(6,2) NOT NULL DEFAULT 0.00 CHECK (qty_tolerance_pct BETWEEN 0 AND 100),
            total_tolerance_abs numeric(14,2) NOT NULL DEFAULT 1.00 CHECK (total_tolerance_abs >= 0),
            approval_tiers jsonb NOT NULL DEFAULT '{TIERS}',
            escalation_hours int NOT NULL DEFAULT 48 CHECK (escalation_hours BETWEEN 1 AND 720)
        )
    """)
    enable_rls("org_ap_settings")
    op.execute("""
        CREATE TABLE matches (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            invoice_id uuid NOT NULL UNIQUE REFERENCES invoices(id) ON DELETE CASCADE,
            po_id uuid REFERENCES purchase_orders(id) ON DELETE SET NULL,
            status text NOT NULL CHECK (status IN ('matched', 'matched_with_variance', 'exception')),
            reasons jsonb NOT NULL DEFAULT '[]',
            manual boolean NOT NULL DEFAULT false,
            matched_by uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE match_lines (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            match_id uuid NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
            invoice_line_id uuid NOT NULL REFERENCES invoice_lines(id) ON DELETE CASCADE,
            po_line_id uuid NOT NULL REFERENCES po_lines(id) ON DELETE CASCADE,
            quantity numeric(14,3) NOT NULL,
            unit_price numeric(14,4),
            price_variance_pct numeric(8,2)
        )
    """)
    op.execute("CREATE INDEX match_lines_po_line_idx ON match_lines (po_line_id)")
    enable_rls("matches")
    enable_rls("match_lines")


def downgrade() -> None:
    for table in ("match_lines", "matches", "org_ap_settings"):
        op.drop_table(table)
