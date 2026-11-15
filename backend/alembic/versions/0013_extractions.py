"""What extraction found for each document: fields with where they are printed (or why they are missing)."""

from alembic import op

from app.rls import enable_rls

revision = "0013"
down_revision = "0012"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE extractions (
            document_id uuid PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            kind text,
            fields jsonb NOT NULL,
            lines jsonb NOT NULL,
            issues jsonb NOT NULL,
            flags jsonb NOT NULL,
            model_used boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    enable_rls("extractions")


def downgrade() -> None:
    op.drop_table("extractions")
