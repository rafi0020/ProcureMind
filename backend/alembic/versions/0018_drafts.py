"""Supplier messages drafted by the Exception Agent. Stored for a person to copy and send; never sent."""

from alembic import op

from app.rls import enable_rls

revision = "0018"
down_revision = "0017"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE drafts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            exception_id uuid NOT NULL REFERENCES exceptions(id) ON DELETE CASCADE,
            run_id uuid REFERENCES runs(id) ON DELETE SET NULL,
            kind text NOT NULL CHECK (kind IN ('credit_note_request', 'corrected_invoice_request')),
            body text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    enable_rls("drafts")


def downgrade() -> None:
    op.drop_table("drafts")
