"""Exceptions: an invoice that failed matching or raised a finding, until a person resolves it."""

from alembic import op

from app.rls import enable_rls

revision = "0016"
down_revision = "0015"
OPEN = "'open','investigating','recommended','escalated'"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE exceptions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            invoice_id uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
            reasons jsonb NOT NULL DEFAULT '[]',
            findings jsonb NOT NULL DEFAULT '[]',
            status text NOT NULL DEFAULT 'open' CHECK (status IN ({OPEN}, 'resolved')),
            recommendation jsonb,
            resolution text CHECK (resolution IN ('approve_with_variance', 'hold', 'request_credit_note',
                                                  'request_corrected_invoice', 'reject_duplicate', 'escalate')),
            note text,
            resolved_by uuid REFERENCES users(id) ON DELETE SET NULL,
            resolved_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(f"CREATE UNIQUE INDEX exceptions_one_open ON exceptions (invoice_id) WHERE status IN ({OPEN})")
    op.execute("CREATE INDEX exceptions_org_idx ON exceptions (org_id, created_at DESC)")
    enable_rls("exceptions")


def downgrade() -> None:
    op.drop_table("exceptions")
