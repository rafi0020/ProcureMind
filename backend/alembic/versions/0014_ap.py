"""Accounts-payable records: purchase orders (with their change history), goods receipts, invoices and credit
notes, their lines, and where on the page each invoice field was found."""

from alembic import op

from app.rls import enable_rls

revision = "0014"
down_revision = "0013"
INVOICE_STATUSES = "'needs_review','ready','matched','exception','pending_approval','approved','rejected','on_hold'"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE purchase_orders (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            vendor_id uuid NOT NULL REFERENCES vendors(id),
            po_number text NOT NULL,
            po_number_norm text NOT NULL,
            currency char(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
            status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
            document_id uuid REFERENCES documents(id) ON DELETE SET NULL,
            created_by uuid REFERENCES users(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (org_id, po_number_norm)
        )
    """)
    op.execute("""
        CREATE TABLE po_lines (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            po_id uuid NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
            line_no int NOT NULL,
            item_code text,
            description text NOT NULL,
            quantity numeric(14,3) NOT NULL CHECK (quantity > 0),
            unit_price numeric(14,4) NOT NULL CHECK (unit_price >= 0),
            UNIQUE (po_id, line_no)
        )
    """)
    op.execute("""
        CREATE TABLE po_changes (
            id bigserial PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            po_id uuid NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
            changed_by uuid REFERENCES users(id) ON DELETE SET NULL,
            at timestamptz NOT NULL DEFAULT now(),
            before jsonb NOT NULL,
            after jsonb NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE goods_receipts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            po_id uuid NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
            receipt_no text NOT NULL,
            received_at timestamptz NOT NULL DEFAULT now(),
            document_id uuid REFERENCES documents(id) ON DELETE SET NULL,
            created_by uuid REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    op.execute("""
        CREATE TABLE receipt_lines (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            receipt_id uuid NOT NULL REFERENCES goods_receipts(id) ON DELETE CASCADE,
            po_line_id uuid NOT NULL REFERENCES po_lines(id) ON DELETE CASCADE,
            quantity numeric(14,3) NOT NULL CHECK (quantity > 0)
        )
    """)
    op.execute("CREATE INDEX receipt_lines_po_line_idx ON receipt_lines (po_line_id)")
    op.execute(f"""
        CREATE TABLE invoices (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            document_id uuid UNIQUE REFERENCES documents(id) ON DELETE SET NULL,
            vendor_id uuid REFERENCES vendors(id),
            vendor_name_printed text,
            kind text NOT NULL CHECK (kind IN ('invoice', 'credit_note')),
            invoice_number text,
            invoice_number_norm text,
            invoice_date date,
            due_date date,
            po_number text,
            currency char(3) CHECK (currency ~ '^[A-Z]{{3}}$'),
            subtotal numeric(14,2),
            tax numeric(14,2),
            total numeric(14,2),
            bank_account text,
            status text NOT NULL DEFAULT 'needs_review' CHECK (status IN ({INVOICE_STATUSES})),
            issues jsonb NOT NULL DEFAULT '[]',
            entered_by uuid REFERENCES users(id) ON DELETE SET NULL,
            corrected_by uuid[] NOT NULL DEFAULT '{{}}',
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX invoices_org_idx ON invoices (org_id, created_at DESC)")
    op.execute("CREATE INDEX invoices_vendor_number_idx ON invoices (vendor_id, invoice_number_norm)")
    op.execute("""
        CREATE TABLE invoice_lines (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            invoice_id uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
            line_no int NOT NULL,
            item_code text,
            description text NOT NULL DEFAULT '',
            quantity numeric(14,3),
            unit_price numeric(14,4),
            amount numeric(14,2),
            UNIQUE (invoice_id, line_no)
        )
    """)
    op.execute("""
        CREATE TABLE field_provenance (
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            invoice_id uuid NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
            field text NOT NULL,
            located boolean NOT NULL,
            page int,
            bbox real[],
            confidence real,
            candidate text,
            reason text,
            PRIMARY KEY (invoice_id, field)
        )
    """)
    for table in (
        "purchase_orders",
        "po_lines",
        "po_changes",
        "goods_receipts",
        "receipt_lines",
        "invoices",
        "invoice_lines",
        "field_provenance",
    ):
        enable_rls(table)


def downgrade() -> None:
    for table in (
        "field_provenance",
        "invoice_lines",
        "invoices",
        "receipt_lines",
        "goods_receipts",
        "po_changes",
        "po_lines",
        "purchase_orders",
    ):
        op.drop_table(table)
