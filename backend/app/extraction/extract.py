"""Extraction: rules and (when configured) the model propose values; only values found on the page are kept.

Per field: both agree and it is on the page -> 0.95; only one is on the page -> 0.7; both on the page but
different -> the rules' value at 0.5, flagged; neither on the page -> Missing, with the rejected candidate kept.
"""

import json
import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from app.db import tenant_session
from app.extraction import arithmetic, llm, parse, provenance, rules
from app.extraction.schema import FIELDS, ExtractedLine, Extraction, Located, Missing
from app.intake.words import PageText
from app.llm.base import LLMError

log = logging.getLogger("procuremind.extraction")


def _decide(field: str, rule_value, model_value, pages: list[PageText]) -> tuple[Located | Missing, str | None]:
    r, m = provenance.typed(field, rule_value), provenance.typed(field, model_value)
    r_at, m_at = provenance.locate(field, r, pages), provenance.locate(field, m, pages)
    if r_at and m_at:
        if (
            parse.code(r) == parse.code(m)
            if field not in ("subtotal", "tax", "total", "doc_date", "due_date")
            else r == m
        ):
            return Located(r, r_at[0], r_at[1], 0.95), None
        return Located(r, r_at[0], r_at[1], 0.5), "disagreement"
    if r_at:
        return Located(r, r_at[0], r_at[1], 0.7), ("model_not_on_page" if m is not None else None)
    if m_at:
        return Located(m, m_at[0], m_at[1], 0.7), None
    if r is None and m is None:
        return Missing("not printed on the document"), None
    return Missing("not found on the page", candidate=model_value if model_value not in (None, "") else rule_value), (
        "model_not_on_page" if m is not None else None
    )


def decide(field: str, rule_value, model_value, pages: list[PageText]) -> Located | Missing:
    return _decide(field, rule_value, model_value, pages)[0]


def _bounded(text, limit=Decimal("1e10")):
    value = parse.amount(text)
    return value if value is not None and abs(value) < limit else None  # quantities and prices are numeric(14,3/4)


def _printed(row: dict, pages: list[PageText]) -> bool:
    quantity, price = _bounded(row.get("quantity")), _bounded(row.get("unit_price"))
    return (
        quantity is not None
        and price is not None
        and bool(provenance.locate("total", quantity, pages) and provenance.locate("total", price, pages))
    )  # the amount test reads a number as it is printed, whichever field it is


def _lines(rows: list[dict], pages: list[PageText]) -> list[ExtractedLine]:
    out = []
    for row in rows:
        amount = provenance.typed("total", row.get("amount"))
        if amount is not None and not provenance.locate("total", amount, pages):
            amount = None  # an amount that is not on the page is not kept
        out.append(ExtractedLine(row.get("item_code") or None, (row.get("description") or "").strip(), _bounded(row.get("quantity")),
                                 _bounded(row.get("unit_price")), amount))  # fmt: skip
    return out


async def extract_pages(pages: list[PageText], kind: str | None, client) -> Extraction:
    proposed = rules.extract(pages, kind)
    from_model, spend, model_failed = {}, None, False
    if client is not None and pages:
        try:
            from_model, spend = await llm.extract(pages, kind, client)
        except LLMError as exc:  # the model is down even after retries: the rules alone read the document
            log.warning("extraction model call failed; rules only", extra={"error": str(exc)[:200]})
            model_failed = True
    result = Extraction(kind=kind or from_model.get("kind"), fields={}, model_used=bool(from_model), spend=spend)
    for name in FIELDS:
        field, flag = _decide(name, proposed["fields"].get(name), from_model.get("fields", {}).get(name), pages)
        result.fields[name] = field
        if flag:
            result.flags[flag].append(name)
    if model_failed:  # shown with the extraction; not a reason for review on its own (the rules read 98 % of fields)
        result.flags["model_unavailable"] = ["The model could not be reached; the rules alone read this document."]
    if proposed["lines"]:
        result.lines = _lines(proposed["lines"], pages)
        result.issues = arithmetic.check(result.fields, result.lines)
        return result
    kept, left_out = [], []
    for row in from_model.get("lines", []):  # the model's lines are kept only where their numbers are printed
        if _printed(row, pages):
            kept.append(row)
        else:
            left_out.append(str(row.get("item_code") or row.get("description") or "unnamed"))
    result.lines = _lines(kept, pages)
    result.issues = arithmetic.check(result.fields, result.lines)
    result.issues += [
        f"Line {name}: the quantity or unit price the model gave is not printed on the document; the line was left out."
        for name in left_out
    ]
    return result


async def extract_step(document_id: UUID) -> None:
    """Pipeline step: extract from the stored pages, keep the result, and set the document's status."""
    from app.intake.steps import load_document, stored_pages

    doc = await load_document(document_id)
    async with tenant_session(doc.org_id) as db:
        kind = await db.scalar(text("SELECT kind FROM documents WHERE id = :d"), {"d": document_id})
    pages = await stored_pages(doc.org_id, document_id)
    result = await extract_pages(pages, kind, await llm.model_client(doc.org_id))
    data = result.as_json()
    async with tenant_session(doc.org_id) as db:
        await db.execute(
            text("INSERT INTO extractions (org_id, document_id, kind, fields, lines, issues, flags, model_used) "
                 "VALUES (:o, :d, :k, CAST(:f AS jsonb), CAST(:l AS jsonb), CAST(:i AS jsonb), CAST(:fl AS jsonb), :m) "
                 "ON CONFLICT (document_id) DO UPDATE SET kind = excluded.kind, fields = excluded.fields, lines = excluded.lines, "
                 "issues = excluded.issues, flags = excluded.flags, model_used = excluded.model_used, created_at = now()"),
            {"o": doc.org_id, "d": document_id, "k": result.kind, "f": json.dumps(data["fields"]), "l": json.dumps(data["lines"]),
             "i": json.dumps(data["issues"]), "fl": json.dumps(data["flags"]), "m": result.model_used},
        )  # fmt: skip
        if result.spend:  # a retried document is charged again, as the provider charges again
            await db.execute(
                text("INSERT INTO model_spend (org_id, document_id, purpose, model, tokens_in, tokens_out, cost_usd, latency_ms) "
                     "VALUES (:o, :d, 'extraction', :m, :i, :t, :c, :l)"),
                {"o": doc.org_id, "d": document_id, "m": result.spend["model"], "i": result.spend["tokens_in"],
                 "t": result.spend["tokens_out"], "c": result.spend["cost_usd"], "l": result.spend["latency_ms"]},
            )  # fmt: skip
        await db.execute(text("UPDATE documents SET status = :s, kind = coalesce(kind, :k) WHERE id = :d"),
                         {"s": result.status, "k": result.kind, "d": document_id})  # fmt: skip
