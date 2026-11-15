"""Indexes for the lists people page through, found by load/explain.py at 100,000 invoices: the purchase-order
list read every order of every organisation and sorted them; the invoice list filtered by status walked the
whole organisation's index to find a few rows."""

from alembic import op

revision = "0019"
down_revision = "0018"


def upgrade() -> None:
    op.execute("CREATE INDEX purchase_orders_org_idx ON purchase_orders (org_id, created_at DESC)")
    op.execute("CREATE INDEX invoices_org_status_idx ON invoices (org_id, status, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX invoices_org_status_idx")
    op.execute("DROP INDEX purchase_orders_org_idx")
