"""Model calls that are not part of a run (document extraction) and their tokens and cost, so budgets and usage see them."""

from alembic import op

from app.rls import enable_rls

revision = "0023"
down_revision = "0022"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE model_spend (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            document_id uuid REFERENCES documents(id) ON DELETE SET NULL,
            purpose text NOT NULL DEFAULT 'extraction' CHECK (purpose IN ('extraction')),
            model text NOT NULL,
            tokens_in int NOT NULL DEFAULT 0,
            tokens_out int NOT NULL DEFAULT 0,
            cost_usd numeric(12, 6) NOT NULL DEFAULT 0,
            latency_ms int NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX model_spend_org_day_idx ON model_spend (org_id, created_at)")
    # The daily limits add up every organisation's spend, so workers' system sessions may read all of it.
    enable_rls("model_spend", system_access=True)


def downgrade() -> None:
    op.drop_table("model_spend")
