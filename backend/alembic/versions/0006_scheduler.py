"""Recurring jobs. One row per job; whichever worker moves `next_run_at` forward runs it."""

from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE scheduled_jobs (
            name text PRIMARY KEY,
            next_run_at timestamptz NOT NULL DEFAULT now(),
            last_started_at timestamptz,
            last_finished_at timestamptz,
            last_error text
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE scheduled_jobs")
