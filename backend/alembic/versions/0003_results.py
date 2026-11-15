"""Evidence: stored query results and calculations that reports cite."""

from alembic import op

from app.rls import enable_rls

revision = "0003"
down_revision = "0001"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE evidence (
            id text PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            run_id uuid,
            kind text NOT NULL CHECK (kind IN ('query', 'compute', 'chart')),
            sql text,
            payload jsonb NOT NULL,
            row_count int NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX evidence_run_idx ON evidence (run_id)")
    enable_rls("evidence")


def downgrade() -> None:
    op.execute("DROP TABLE evidence")
