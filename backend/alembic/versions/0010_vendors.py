"""Vendors, the names their documents use, and their bank accounts with a two-person change process."""

from alembic import op

from app.rls import enable_rls

revision = "0010"
down_revision = "0006"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE vendors (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            tax_id text,
            email text,
            default_currency char(3) NOT NULL CHECK (default_currency ~ '^[A-Z]{3}$'),
            payment_terms_days int NOT NULL DEFAULT 30 CHECK (payment_terms_days BETWEEN 0 AND 365),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE UNIQUE INDEX vendors_name_key ON vendors (org_id, lower(name))")
    enable_rls("vendors")
    op.execute("""
        CREATE TABLE vendor_aliases (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            vendor_id uuid NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
            alias text NOT NULL CHECK (length(alias) BETWEEN 1 AND 200)
        )
    """)
    op.execute("CREATE UNIQUE INDEX vendor_aliases_key ON vendor_aliases (org_id, lower(alias))")
    enable_rls("vendor_aliases")
    op.execute("""
        CREATE TABLE vendor_bank_accounts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            vendor_id uuid NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
            iban_or_account text NOT NULL CHECK (length(iban_or_account) BETWEEN 4 AND 64),
            bank_name text NOT NULL,
            status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'retired', 'rejected')),
            requested_by uuid REFERENCES users(id) ON DELETE SET NULL,
            decided_by uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            decided_at timestamptz
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX vendor_one_active_account ON vendor_bank_accounts (vendor_id) WHERE status = 'active'"
    )
    enable_rls("vendor_bank_accounts")


def downgrade() -> None:
    for table in ("vendor_bank_accounts", "vendor_aliases", "vendors"):
        op.drop_table(table)
