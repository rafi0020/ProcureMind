"""Approval decisions on invoices, and when each invoice started waiting (for escalation)."""

from alembic import op

from app.rls import enable_rls

revision = "0017"
down_revision = "0016"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE invoice_approvals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            invoice_id uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
            approver_id uuid NOT NULL REFERENCES users(id),
            approver_role text NOT NULL,
            decision text NOT NULL CHECK (decision IN ('approved', 'rejected')),
            reason text,
            at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (invoice_id, approver_id)
        )
    """)
    enable_rls("invoice_approvals")
    op.execute("ALTER TABLE invoices ADD COLUMN pending_since timestamptz, ADD COLUMN escalated_at timestamptz")
    # Whichever code path moves an invoice into approval, the clock starts here.
    op.execute("""
        CREATE FUNCTION invoices_pending_since() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status = 'pending_approval' AND OLD.status IS DISTINCT FROM 'pending_approval' THEN
                NEW.pending_since := now();
                NEW.escalated_at := NULL;
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER invoices_pending_since BEFORE UPDATE OF status ON invoices FOR EACH ROW EXECUTE FUNCTION invoices_pending_since()"
    )
    op.execute(
        "CREATE INDEX invoices_pending_idx ON invoices (org_id, pending_since) WHERE status = 'pending_approval'"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER invoices_pending_since ON invoices")
    op.execute("DROP FUNCTION invoices_pending_since()")
    op.execute("ALTER TABLE invoices DROP COLUMN pending_since, DROP COLUMN escalated_at")
    op.drop_table("invoice_approvals")
