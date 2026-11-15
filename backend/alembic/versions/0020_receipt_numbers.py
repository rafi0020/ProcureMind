"""A receipt number is unique per purchase order: the same receipt twice is one receipt."""

from alembic import op

revision = "0020"
down_revision = "0019"


def upgrade() -> None:
    op.execute("ALTER TABLE goods_receipts ADD CONSTRAINT goods_receipts_po_receipt_no_key UNIQUE (po_id, receipt_no)")


def downgrade() -> None:
    op.execute("ALTER TABLE goods_receipts DROP CONSTRAINT goods_receipts_po_receipt_no_key")
