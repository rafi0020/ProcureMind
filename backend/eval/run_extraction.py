"""Field accuracy of extraction on documents the rules were never tuned on (generator seed 8, both organisations).

    python -m eval.run_extraction                     # rules alone
    python -m eval.run_extraction --model cc/claude-sonnet-5-5 --limit 12

Each planned invoice is rendered four ways (digital PDF, clean scan, medium scan, phone photo), so every quality is
measured on the same documents. Per field the outcome is one of:
  correct   the stored value equals what was printed
  missing   nothing was stored (a person fills it in)
  misread   a value was stored, it is wrong, and it is text found on the page (OCR or a wrong label)
  invented  a value was stored that is not on the page at all. This must be 0: extraction only stores values it
            located on the page, and this measures that guarantee from outside.
Needs Tesseract for the scans (the backend image has it); run it in the tests container.
"""

import argparse
import asyncio
import json
import re
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.extraction.extract import extract_pages
from app.extraction.schema import FIELDS, Located
from app.intake.classify import classify
from app.intake.text import page_texts
from seed.demo_users import KESTREL, RIDGEWAY
from seed.generate import _render, doc_spec, plan

HERE = Path(__file__).parent
QUALITIES = ("digital", "clean", "medium", "photo")
MIME = {"pdf": "application/pdf", "jpg": "image/jpeg"}


def truth_of(spec) -> dict:
    return {"doc_number": spec.number, "doc_date": spec.doc_date, "due_date": spec.due_date, "vendor_name": spec.vendor_name,
            "vendor_tax_id": spec.vendor_tax_id, "po_number": spec.po_number, "currency": spec.currency, "subtotal": spec.subtotal,
            "tax": spec.tax, "total": spec.total, "bank_account": spec.bank_account}  # fmt: skip


def same(field: str, got, want) -> bool:
    if want is None:
        return got is None
    if isinstance(want, Decimal | date):
        return got == want
    return re.sub(r"\s+", "", str(got)).casefold() == re.sub(r"\s+", "", str(want)).casefold()


def on_page(value, pages) -> bool:
    """Whether a value's printed form occurs on the page: its characters, or the number in any common formatting."""
    words = " ".join(w.text for p in pages for w in p.words)
    flat = re.sub(r"[\s,]", "", words).casefold()
    if isinstance(value, date):
        return any(
            value.strftime(f) in words
            for f in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%b %d, %Y")
        )
    return re.sub(r"[\s,]", "", f"{value:f}" if isinstance(value, Decimal) else str(value)).casefold() in flat


def grade(extraction, truth: dict, pages) -> dict[str, str]:
    out = {}
    for name in FIELDS:
        f = extraction.fields.get(name)
        if not isinstance(f, Located):
            out[name] = "missing" if truth[name] is not None else "correct"
        elif same(name, f.value, truth[name]):
            out[name] = "correct"
        else:
            out[name] = "misread" if on_page(f.value, pages) else "invented"
    return out


async def run(args) -> dict:
    data = plan(seed=8, scale=args.scale)
    docs = [
        (org, data["orgs"][str(org)], d) for org in (KESTREL, RIDGEWAY) for d in data["orgs"][str(org)]["documents"]
    ]
    docs = docs[: args.limit] if args.limit else docs
    client = None
    if args.model:
        from app.llm import factory
        from eval.common import configure_model

        configure_model(args.model)
        client = await factory.get_client(KESTREL)
    rows = []
    for n, (org, org_plan, doc) in enumerate(docs):
        spec = doc_spec(org, org_plan, doc)
        truth = truth_of(spec)
        for quality in QUALITIES:
            ext, body = _render(spec, quality, seed=1000 + n)
            pages = await asyncio.to_thread(page_texts, body, MIME[ext])
            kind, _ = classify(pages)
            extraction = await extract_pages(pages, kind, client)
            rows.append({"document": doc["filename"], "quality": quality, "kind": kind, "fields": grade(extraction, truth, pages),
                         "status": extraction.status, "lines": len(extraction.lines), "expected_lines": len(spec.lines)})  # fmt: skip
        print(f"  {n + 1}/{len(docs)} {doc['filename']}")
    return summarise(rows, args)


def summarise(rows: list[dict], args) -> dict:
    def rate(subset, outcome="correct"):
        cells = [o for r in subset for o in r["fields"].values()]
        return round(sum(o == outcome for o in cells) / max(1, len(cells)), 4)

    by_quality = {q: {"documents": sum(r["quality"] == q for r in rows), "field_accuracy": rate([r for r in rows if r["quality"] == q]),
                      "missing": rate([r for r in rows if r["quality"] == q], "missing"), "misread": rate([r for r in rows if r["quality"] == q], "misread"),
                      "lines_complete": round(sum(r["lines"] == r["expected_lines"] for r in rows if r["quality"] == q) / max(1, sum(r["quality"] == q for r in rows)), 4),
                      "classified_invoice": round(sum(r["kind"] == "invoice" for r in rows if r["quality"] == q) / max(1, sum(r["quality"] == q for r in rows)), 4)}
                  for q in QUALITIES}  # fmt: skip
    by_field = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for name, outcome in r["fields"].items():
            by_field[name][outcome] += 1
    invented = sum(o == "invented" for r in rows for o in r["fields"].values())
    return {"name": f"extraction-{'rules' if not args.model else 'rules+' + args.model.replace('/', '-')}", "mode": "rules" if not args.model else "rules+model",
            "model": args.model or None, "seed": 8, "at": datetime.now(UTC).isoformat(timespec="seconds"), "documents": len(rows) // len(QUALITIES),
            "field_accuracy": rate(rows), "invented": invented, "by_quality": by_quality, "by_field": {k: dict(v) for k, v in by_field.items()}, "rows": rows}  # fmt: skip


async def main(args) -> None:
    result = await run(args)
    print(json.dumps({k: result[k] for k in ("field_accuracy", "invented", "by_quality", "by_field")}, indent=1))
    out = HERE / "results" / f"{result['name']}.json"
    out.write_text(json.dumps(result, indent=1, default=str))
    print(f"written to {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extraction field accuracy on held-out documents")
    parser.add_argument("--model", default="")
    parser.add_argument("--scale", type=float, default=0.15)
    parser.add_argument("--limit", type=int, default=0)
    asyncio.run(main(parser.parse_args()))
