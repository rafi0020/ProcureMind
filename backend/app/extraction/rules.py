"""The rule-based extractor: labels anchor values, a header row anchors the line table.

Free and deterministic: a second opinion for the model and the whole extractor when no model is configured."""

import re

from app.extraction import parse
from app.intake.words import PageText, Word

LABELS = {  # field -> label spellings, as word sequences (lower case, trailing ':' ignored)
    "doc_number": ["invoice number", "invoice no", "invoice #", "invoice no.", "bill no", "credit note no", "credit note number",
                   "document no", "grn no", "receipt no"],
    "doc_date": ["invoice date", "credit note date", "date", "issued", "order date"],
    "due_date": ["due date", "payment due", "pay by", "due"],
    "po_number": ["po number", "your order", "purchase order", "p.o.", "order no", "order number"],
    "total": ["total due", "amount due", "grand total", "balance due", "total"],
    "subtotal": ["subtotal", "sub-total", "net"],
    "tax": ["sales tax", "vat", "gst", "tax"],
    "bank_account": ["pay to account", "bank account", "iban", "account no"],
    "vendor_tax_id": ["tax id", "vat no", "vat number", "tax number"],
    "supplier": ["supplier", "vendor"],
}  # fmt: skip
TITLES = {
    "invoice",
    "tax",
    "credit",
    "note",
    "purchase",
    "order",
    "goods",
    "received",
    "receipt",
    "grn",
    "statement",
    "of",
    "account",
}
ALL_LABELS = sorted(
    ((tuple(label.split()), name) for name, labels in LABELS.items() for label in labels), key=lambda x: -len(x[0])
)


def clean(word: str) -> str:
    return word.lower().rstrip(":")


def lines_of(page: PageText) -> list[list[Word]]:
    """Words grouped into printed lines (same baseline within a few units), left to right."""
    lines: list[list[Word]] = []
    for w in sorted(page.words, key=lambda w: (w.y0, w.x0)):
        if lines and abs(lines[-1][0].y0 - w.y0) <= max(3.0, (w.y1 - w.y0) * 0.4):
            lines[-1].append(w)
        else:
            lines.append([w])
    return [sorted(line, key=lambda w: w.x0) for line in lines]


def label_at_start(line: list[Word]) -> tuple[str, int] | None:
    tokens = [clean(w.text) for w in line]
    for label, name in ALL_LABELS:
        if tuple(tokens[: len(label)]) == label:
            return name, len(label)
    return None


def first_line_vendor(page: PageText) -> str | None:
    """The issuer's name: the first line above the labelled fields with words other than the title."""
    for line in lines_of(page)[:4]:
        if label_at_start(line):
            break
        rest = [w.text for w in line if w.text.lower() not in TITLES]
        if rest:
            return " ".join(rest)
    return None


def table(lines: list[list[Word]]) -> list[dict]:
    """Rows of the line-item table: below a header naming quantity and amount, until totals or a blank row."""
    for i, line in enumerate(lines):
        heads = [clean(w.text) for w in line]
        if any(h in ("qty", "quantity") for h in heads) and any(h in ("amount", "total", "line total") for h in heads):
            columns = []
            for w in line:
                h = clean(w.text)
                name = {"item": "item_code", "code": "item_code", "sku": "item_code", "description": "description", "qty": "quantity",
                        "quantity": "quantity", "unit": "unit_price", "price": "unit_price", "rate": "unit_price", "amount": "amount", "total": "amount"}.get(h)  # fmt: skip
                if name and name not in [c[1] for c in columns]:
                    columns.append((w.x0, name))
            rows = []
            for row in lines[i + 1 :]:
                if label_at_start(row) and label_at_start(row)[0] in ("subtotal", "total", "tax"):
                    break
                cells: dict[str, list[str]] = {}
                for w in row:
                    column = max((c for c in columns if c[0] <= w.x0 + 4), default=columns[0], key=lambda c: c[0])
                    cells.setdefault(column[1], []).append(w.text)
                values = {k: " ".join(v) for k, v in cells.items()}
                if parse.amount(values.get("amount")) is None:
                    break
                rows.append(values)
            return rows
    return []


def extract(pages: list[PageText], kind: str | None) -> dict:
    """Candidate values as printed, field by field, plus line rows. Nothing here is checked yet."""
    found: dict[str, str] = {}
    if not pages:
        return {"fields": found, "lines": []}
    for page in pages:
        lines = lines_of(page)
        for n, line in enumerate(lines):
            if line and all(w.text.lower().strip(":") in TITLES for w in line):
                continue  # the document's title ("PURCHASE ORDER"), not a label with its value on the next line
            hit = label_at_start(line)
            if not hit:
                continue
            name, length = hit
            if kind == "purchase_order" and name == "po_number":
                name = "doc_number"  # on a purchase order, "PO Number" is the document's own number
            value = " ".join(w.text for w in line[length:]).lstrip(":").strip()
            if not value and n + 1 < len(lines) and not label_at_start(lines[n + 1]):
                value = " ".join(w.text for w in lines[n + 1])  # the value printed under its label
            if value and name not in found:
                found[name] = value
        table_rows = table(lines)
        if table_rows:
            found.setdefault("_lines", table_rows)
    out = {k: v for k, v in found.items() if not k.startswith("_")}
    for name in ("total", "subtotal", "tax"):
        if name in out:  # "10%: 9.30 USD" -> the last number that is not a percentage
            numbers = [t for t in out[name].split() if parse.amount(t) is not None and not t.rstrip(":").endswith("%")]
            out[name] = numbers[-1] if numbers else ""
    out["currency"] = parse.currency(found.get("total", "")) or next(
        (
            c
            for p in pages
            for w in p.words
            if (c := parse.currency(w.text)) and len(re.sub(r"[^A-Za-z]", "", w.text)) == 3
        ),
        None,
    )
    vendor = out.pop("supplier", None) if kind == "purchase_order" else first_line_vendor(pages[0])
    out.pop("supplier", None)
    if vendor:
        out["vendor_name"] = vendor
    return {"fields": {k: v for k, v in out.items() if v}, "lines": found.get("_lines", [])}
