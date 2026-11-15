"""The words of every page of every document, with their positions: read once, used by extraction, the review
screen (highlighting a field's box) and the agent's document_text tool."""

from alembic import op

from app.rls import enable_rls

revision = "0012"
down_revision = "0011"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE document_pages (
            org_id uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            page int NOT NULL,
            source text NOT NULL CHECK (source IN ('text_layer', 'ocr', 'sheet')),
            width real NOT NULL,
            height real NOT NULL,
            text text NOT NULL,
            words jsonb NOT NULL,
            PRIMARY KEY (document_id, page)
        )
    """)
    enable_rls("document_pages")


def downgrade() -> None:
    op.drop_table("document_pages")
