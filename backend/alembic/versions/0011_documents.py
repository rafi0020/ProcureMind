"""Documents (each one also a queue entry for the workers) and inbox keys."""

from alembic import op

from app.rls import enable_rls

revision = "0011"
down_revision = "0010"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE documents (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            filename text NOT NULL,
            content_type text NOT NULL,
            size_bytes int NOT NULL,
            sha256 char(64) NOT NULL,
            pages int,
            kind text CHECK (kind IN ('invoice', 'credit_note', 'purchase_order', 'goods_receipt', 'statement', 'other')),
            status text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'processing', 'extracted', 'needs_review', 'failed', 'dead')),
            attempts int NOT NULL DEFAULT 0,
            lease_owner text,
            lease_expires_at timestamptz,
            error text,
            source text NOT NULL DEFAULT 'upload' CHECK (source IN ('upload', 'inbox')),
            uploaded_by uuid REFERENCES users(id) ON DELETE SET NULL,
            received_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, sha256)
        )
    """)
    op.execute("CREATE INDEX documents_queue_idx ON documents (received_at) WHERE status IN ('queued', 'processing')")
    op.execute("CREATE INDEX documents_org_idx ON documents (org_id, received_at DESC)")
    enable_rls("documents", system_access=True)  # workers claim documents across organisations
    op.execute("""
        CREATE TABLE inbox_keys (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            name text NOT NULL,
            key_hash char(64) NOT NULL UNIQUE,
            prefix text NOT NULL,
            created_by uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz
        )
    """)
    # A key arrives before anything is known about its organisation, so it is looked up by hash in a system session.
    enable_rls("inbox_keys", system_access=True)


def downgrade() -> None:
    op.drop_table("inbox_keys")
    op.drop_table("documents")
