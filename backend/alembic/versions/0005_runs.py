"""Agent runs, their steps, and approvals.

A run is also its own queue entry: workers claim rows of `runs` (see app/worker/run.py). Every model call and
tool call is a row of `steps`, committed before the next one starts, which is what makes a run resumable.
"""

from alembic import op

from app.rls import enable_rls

revision = "0005"
down_revision = "0004"

STATUSES = "'queued','running','waiting_approval','waiting_user','completed','failed','cancelled','budget_exceeded'"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            user_id uuid REFERENCES users(id) ON DELETE SET NULL,
            role text NOT NULL,
            question text NOT NULL,
            status text NOT NULL DEFAULT 'queued' CHECK (status IN ({STATUSES})),
            result jsonb,
            error text,
            pending jsonb,
            steps_used int NOT NULL DEFAULT 0,
            tokens_used int NOT NULL DEFAULT 0,
            cost_usd numeric(12,6) NOT NULL DEFAULT 0,
            active_ms int NOT NULL DEFAULT 0,
            cancel_requested boolean NOT NULL DEFAULT false,
            lease_owner text,
            lease_expires_at timestamptz,
            attempts int NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz
        )
    """)
    # The claim query: oldest queued run, or a running one whose lease has expired.
    op.execute("CREATE INDEX runs_claim_idx ON runs (created_at) WHERE status IN ('queued', 'running')")
    op.execute("CREATE INDEX runs_org_created_idx ON runs (org_id, created_at DESC)")
    op.execute("CREATE INDEX runs_org_active_idx ON runs (org_id) WHERE status IN ('queued', 'running')")
    enable_rls("runs", system_access=True)

    op.execute("""
        CREATE TABLE steps (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            idx int NOT NULL,
            kind text NOT NULL CHECK (kind IN ('model', 'tool', 'note')),
            name text NOT NULL,
            state text NOT NULL DEFAULT 'done'
                CHECK (state IN ('started', 'awaiting_approval', 'awaiting_user', 'done')),
            call_id text,
            input jsonb,
            output jsonb,
            error text,
            tokens_in int NOT NULL DEFAULT 0,
            tokens_out int NOT NULL DEFAULT 0,
            cost_usd numeric(12,6) NOT NULL DEFAULT 0,
            latency_ms int NOT NULL DEFAULT 0,
            idempotency_key text NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            UNIQUE (run_id, idx)
        )
    """)
    enable_rls("steps")

    op.execute("""
        CREATE TABLE approvals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            step_idx int NOT NULL,
            tool text NOT NULL,
            arguments jsonb NOT NULL,
            status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
            requested_by uuid REFERENCES users(id) ON DELETE SET NULL,
            decided_by uuid REFERENCES users(id) ON DELETE SET NULL,
            decided_at timestamptz,
            reason text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (run_id, step_idx)
        )
    """)
    op.execute("CREATE INDEX approvals_org_status_idx ON approvals (org_id, status, created_at DESC)")
    enable_rls("approvals")
    op.execute(
        "ALTER TABLE evidence ADD CONSTRAINT evidence_run_fk FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE evidence DROP CONSTRAINT evidence_run_fk")
    op.execute("DROP TABLE approvals, steps, runs")
