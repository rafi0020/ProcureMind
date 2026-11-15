"""What extraction produces. A field is Located (found on the page, with where) or Missing (with why)."""

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

FIELDS = ("doc_number", "doc_date", "due_date", "vendor_name", "vendor_tax_id", "po_number", "currency",
          "subtotal", "tax", "total", "bank_account")  # fmt: skip
AMOUNTS = ("subtotal", "tax", "total")
DATES = ("doc_date", "due_date")
REQUIRED = ("doc_number", "doc_date", "vendor_name", "currency", "total")


@dataclass
class Located:
    value: Any
    page: int
    bbox: tuple[float, float, float, float]
    confidence: float


@dataclass
class Missing:
    reason: str
    candidate: Any = None


@dataclass
class ExtractedLine:
    item_code: str | None
    description: str
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal | None


@dataclass
class Extraction:
    kind: str | None
    fields: dict[str, Located | Missing]
    lines: list[ExtractedLine] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    flags: dict[str, list[str]] = field(default_factory=lambda: {"disagreement": [], "model_not_on_page": []})
    model_used: bool = False
    spend: dict | None = None  # what the model call cost (tokens, USD), when one was made; kept with the document

    @property
    def status(self) -> str:
        complete = all(isinstance(self.fields.get(k), Located) for k in REQUIRED)
        disputed = bool(self.flags.get("disagreement"))
        return "extracted" if complete and not self.issues and not disputed else "needs_review"

    def as_json(self) -> dict:
        def plain(v):
            if isinstance(v, Decimal):
                return str(v)
            if isinstance(v, date):
                return v.isoformat()
            return v

        fields = {}
        for name, f in self.fields.items():
            d = asdict(f)
            d = {k: plain(v) for k, v in d.items()}
            d["located"] = isinstance(f, Located)
            fields[name] = d
        lines = [{k: plain(v) for k, v in asdict(ln).items()} for ln in self.lines]
        return {
            "kind": self.kind,
            "fields": fields,
            "lines": lines,
            "issues": self.issues,
            "flags": self.flags,
            "model_used": self.model_used,
        }
