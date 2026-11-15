"""A run can be started for one exception; the exception agent may then recommend only on that exception."""

from alembic import op

revision = "0022"
down_revision = "0021"


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN exception_id uuid REFERENCES exceptions(id) ON DELETE SET NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP COLUMN exception_id")
