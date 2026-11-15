"""An exception can be closed by a clean match, not only decided by a person."""

from alembic import op

revision = "0021"
down_revision = "0020"
RESOLUTIONS = (
    "'approve_with_variance', 'hold', 'request_credit_note', 'request_corrected_invoice', 'reject_duplicate', "
    "'escalate', 'cleared_by_rematch'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE exceptions DROP CONSTRAINT exceptions_resolution_check")
    op.execute(
        f"ALTER TABLE exceptions ADD CONSTRAINT exceptions_resolution_check CHECK (resolution IN ({RESOLUTIONS}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE exceptions DROP CONSTRAINT exceptions_resolution_check")
    op.execute(
        "ALTER TABLE exceptions ADD CONSTRAINT exceptions_resolution_check CHECK (resolution IN "
        "('approve_with_variance', 'hold', 'request_credit_note', 'request_corrected_invoice', 'reject_duplicate', 'escalate'))"
    )
