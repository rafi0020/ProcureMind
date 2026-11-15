"""What kind of document this is, from its heading and wording. Unsure means None (the model may decide later)."""

import re

from app.intake.words import PageText

RULES = [  # (pattern on the top of page 1, kind, confidence); the first match wins
    (r"\bcredit\s*(note|memo)\b", "credit_note", 0.95),
    (r"\b(goods\s+received|goods\s+receipt|delivery\s+note|receiving\s+report)\b", "goods_receipt", 0.9),
    (r"\bstatement\s+of\s+account\b", "statement", 0.9),
    (r"\bpurchase\s+order\b", "purchase_order", 0.85),
    (r"\b(tax\s+)?invoice\b|\bbill\s+no\b", "invoice", 0.9),
]

REFERENCE = re.compile(r"\bpurchase\s+order\s*(no\.?|number|#|ref\.?)?\s*:")


def _match(text: str) -> tuple[str | None, float]:
    for pattern, kind, confidence in RULES:
        if re.search(pattern, text):
            return kind, confidence
    return None, 0.0


def classify(pages: list[PageText]) -> tuple[str | None, float]:
    if not pages or not pages[0].words:
        return None, 0.0
    first = pages[0]
    top = [w for w in sorted(first.words, key=lambda w: (w.y0, w.x0)) if w.y0 <= first.height / 4]
    if not top:
        return None, 0.0
    # The printed title decides: it is the largest text at the top. An invoice may label its PO reference
    # "Purchase Order:" a few lines lower, inside the same top quarter.
    tallest = max(w.y1 - w.y0 for w in top)
    title = " ".join(w.text for w in top if (w.y1 - w.y0) >= 0.8 * tallest).lower()
    kind, confidence = _match(title)
    if kind:
        return kind, confidence
    # No readable title (a misread scan): use the wording, but a "Purchase Order:" or "PO No." label is a reference
    # to an order, as printed on invoices, not a heading.
    body = REFERENCE.sub(" ", " ".join(w.text for w in top).lower())
    kind, confidence = _match(body)
    return kind, round(confidence * 0.9, 2) if kind else 0.0
