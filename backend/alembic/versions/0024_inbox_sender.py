"""The sender and subject of an e-mail that brought a document through the inbox."""

from alembic import op

revision = "0024"
down_revision = "0023"


def upgrade() -> None:
    op.execute("ALTER TABLE documents ADD COLUMN inbox_from text, ADD COLUMN inbox_subject text")


def downgrade() -> None:
    op.execute("ALTER TABLE documents DROP COLUMN inbox_from, DROP COLUMN inbox_subject")
