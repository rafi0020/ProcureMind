"""Demo documents for tests, rendered by seed/documents.py."""

from datetime import date
from decimal import Decimal as D

from seed.documents import DocSpec, Line


def invoice(number="INV-1001", po="PO-00123", kind="invoice", layout=0, note=None, lines=None, **extra) -> DocSpec:
    values = dict(
        kind=kind, number=number, doc_date=date(2026, 10, 3), due_date=date(2026, 11, 2), vendor_name="Acme Supplies Ltd",
        buyer_name="Kestrel Foods", po_number=po, vendor_tax_id="GB123456789", bank_account="GB33BUKB20201555555555",
        lines=lines or [Line("W-1", "Widget, steel", D(10), D("4.50")), Line("B-7", "Bracket", D(4), D("12.00"))],
        layout=layout, note=note,
    )  # fmt: skip
    return DocSpec(**(values | extra))
