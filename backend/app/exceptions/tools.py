"""The Exception Agent's tools: typed, read-only reads of AP records, and two that only store things.

Values printed by suppliers (invoice fields, line descriptions, page text, PO line descriptions) are returned
under `untrusted_data`: what an outside party wrote, never instructions. No tool changes an invoice, an
approval or a vendor; `recommend` stores a recommendation for a person and `draft_supplier_message` a draft.
"""

import json
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import text

from app.agent.tools import ToolContext, ToolError, ToolResult
from app.db import tenant_session
from app.exceptions.common import PREVIEW_ROWS, _evidence, _store, registry
from app.models.runs import new_handle

OUTCOMES = Literal[
    "approve_with_variance", "hold", "request_credit_note", "request_corrected_invoice", "reject_duplicate", "escalate"
]


def _num(v):
    return (
        float(v)
        if isinstance(v, Decimal)
        else (v.isoformat() if hasattr(v, "isoformat") else (str(v) if isinstance(v, UUID) else v))
    )


def _rows(result) -> list[dict]:
    return [{k: _num(v) for k, v in r._mapping.items()} for r in result]


def _result(
    ctx: ToolContext, summary: str, rows: list[dict], purpose: str, *, untrusted: bool = True, extra: dict | None = None
) -> ToolResult:
    handle = new_handle()
    columns = list(rows[0]) if rows else []
    shown = rows[:PREVIEW_ROWS]
    data = ({"untrusted_data": shown} if untrusted else {"rows": shown}) | (extra or {})
    return ToolResult(summary, data=data, handle=handle,
                      commit=_store(ctx, handle, "query", {"columns": columns, "rows": [[r.get(c) for c in columns] for r in rows], "purpose": purpose,
                                                           "extra": extra or {}}, rows=len(rows)))  # what the model saw is what its figures are checked against  # fmt: skip


class ExceptionArgs(BaseModel):
    exception_id: UUID


class InvoiceArgs(BaseModel):
    invoice_id: UUID


class PoArgs(BaseModel):
    po_number: str = Field(max_length=60)


@registry.tool(
    "get_exception", "Read an exception: why the invoice was stopped (match reasons and findings) and its invoice id."
)
async def get_exception(ctx: ToolContext, args: ExceptionArgs) -> ToolResult:
    async with tenant_session(ctx.org_id) as db:
        row = (
            await db.execute(
                text("SELECT id, invoice_id, status, reasons, findings FROM exceptions WHERE id = :e"),
                {"e": args.exception_id},
            )
        ).first()
    if row is None:
        raise ToolError("No exception with this id in this organisation.")
    rows = [{"kind": "reason", "code": r["code"], "detail": r["detail"]} for r in row.reasons] + [
        {"kind": "finding", "code": f["code"], "detail": f["reason"], "score": f["score"]} for f in row.findings]  # fmt: skip
    return _result(ctx, f"Exception {args.exception_id} read; status {row.status}.", rows, "exception",
                   extra={"invoice_id": str(row.invoice_id), "status": row.status})  # fmt: skip


@registry.tool("get_invoice", "Read an invoice or credit note: its fields as extracted, its lines, and its status.")
async def get_invoice(ctx: ToolContext, args: InvoiceArgs) -> ToolResult:
    async with tenant_session(ctx.org_id) as db:
        inv = (await db.execute(text("""SELECT i.id, i.kind, i.invoice_number, i.invoice_date, i.po_number, i.currency, i.subtotal, i.tax, i.total,
                                               i.bank_account, i.status, v.name AS vendor FROM invoices i LEFT JOIN vendors v ON v.id = i.vendor_id WHERE i.id = :i"""),
                                {"i": args.invoice_id})).first()  # fmt: skip
        if inv is None:
            raise ToolError("No invoice with this id in this organisation.")
        lines = _rows(
            await db.execute(
                text(
                    "SELECT line_no, item_code, description, quantity, unit_price, amount FROM invoice_lines WHERE invoice_id = :i ORDER BY line_no"
                ),
                {"i": args.invoice_id},
            )
        )
    header = {k: _num(v) for k, v in inv._mapping.items()}
    return _result(ctx, f"Invoice record read: {len(lines)} line(s), status {inv.status}.",
                   [header | {"line": None}] + [{"line": ln["line_no"], **{k: v for k, v in ln.items() if k != "line_no"}} for ln in lines], f"invoice {inv.invoice_number}")  # fmt: skip


@registry.tool("get_po", "Read a purchase order: its lines and every change made to it.")
async def get_po(ctx: ToolContext, args: PoArgs) -> ToolResult:
    from app.ap.numbers import normalise_number

    async with tenant_session(ctx.org_id) as db:
        po = (await db.execute(text("SELECT p.id, p.po_number, p.currency, p.status, p.created_at, v.name AS vendor FROM purchase_orders p JOIN vendors v ON v.id = p.vendor_id WHERE p.po_number_norm = :n"),
                               {"n": normalise_number(args.po_number)})).first()  # fmt: skip
        if po is None:
            raise ToolError(f"No purchase order {args.po_number} in this organisation.")
        lines = _rows(
            await db.execute(
                text(
                    "SELECT line_no, item_code, description, quantity, unit_price FROM po_lines WHERE po_id = :p ORDER BY line_no"
                ),
                {"p": po.id},
            )
        )
        changes = (
            await db.execute(
                text("SELECT at, before, after FROM po_changes WHERE po_id = :p ORDER BY at"), {"p": po.id}
            )
        ).all()
    change_rows = [
        {"section": "change", "at": c.at.isoformat(), "before": c.before.get("lines"), "after": c.after.get("lines")}
        for c in changes
    ]
    return _result(ctx, f"Purchase order read: {len(lines)} line(s), {len(changes)} change(s), status {po.status}.", lines + change_rows,
                   f"PO {po.po_number}")  # fmt: skip


@registry.tool("get_receipts", "Read what was received against a purchase order, per line.")
async def get_receipts(ctx: ToolContext, args: PoArgs) -> ToolResult:
    from app.ap.numbers import normalise_number

    async with tenant_session(ctx.org_id) as db:
        rows = _rows(await db.execute(text("""
            SELECT g.receipt_no, g.received_at, pl.line_no, pl.item_code, rl.quantity
            FROM goods_receipts g JOIN purchase_orders p ON p.id = g.po_id JOIN receipt_lines rl ON rl.receipt_id = g.id JOIN po_lines pl ON pl.id = rl.po_line_id
            WHERE p.po_number_norm = :n ORDER BY g.received_at, pl.line_no"""), {"n": normalise_number(args.po_number)}))  # fmt: skip
    return _result(ctx, f"{len(rows)} receipt line(s) found." if rows else "Nothing has been received against this order.", rows,
                   f"receipts for {args.po_number}")  # fmt: skip


class VendorArgs(BaseModel):
    vendor: str = Field(max_length=200, description="Vendor name as on the invoice")
    days: int = Field(default=180, ge=1, le=365)


@registry.tool(
    "vendor_history", "A vendor's invoices, prices by item, bank accounts on file and accounts printed on its invoices."
)
async def vendor_history(ctx: ToolContext, args: VendorArgs) -> ToolResult:
    from app.vendors import find_vendor

    async with tenant_session(ctx.org_id) as db:
        vendor = await find_vendor(db, args.vendor)
        if vendor is None:
            raise ToolError(f"No vendor called {args.vendor} in this organisation.")
        since = {"v": vendor.id, "d": f"{args.days} days"}
        invoices = _rows(await db.execute(text("""SELECT invoice_number, kind, invoice_date, total, status FROM invoices
                                                  WHERE vendor_id = :v AND invoice_date >= current_date - CAST(:d AS interval) ORDER BY invoice_date DESC LIMIT 50"""), since))  # fmt: skip
        prices = _rows(await db.execute(text("""SELECT il.item_code, min(il.unit_price) AS lowest, max(il.unit_price) AS highest, count(*) AS times
                                                FROM invoice_lines il JOIN invoices i ON i.id = il.invoice_id
                                                WHERE i.vendor_id = :v AND i.status IN ('approved', 'pending_approval', 'matched') AND il.item_code IS NOT NULL
                                                AND i.invoice_date >= current_date - CAST(:d AS interval) GROUP BY il.item_code ORDER BY il.item_code"""), since))  # fmt: skip
        accounts = _rows(
            await db.execute(
                text(
                    "SELECT iban_or_account, status, created_at, decided_at FROM vendor_bank_accounts WHERE vendor_id = :v ORDER BY created_at"
                ),
                {"v": vendor.id},
            )
        )
        printed = _rows(
            await db.execute(
                text(
                    "SELECT bank_account, count(*) AS invoices FROM invoices WHERE vendor_id = :v AND bank_account IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
                ),
                {"v": vendor.id},
            )
        )
    # accounts first: the preview shows the first rows only, and they are what a person is checking
    rows = (
        [{"section": "account_on_file", **r} for r in accounts]
        + [{"section": "account_printed_on_invoices", **r} for r in printed]
        + [{"section": "invoice", **r} for r in invoices]
        + [{"section": "price", **r} for r in prices]
    )
    return _result(ctx, f"Vendor history read: {len(invoices)} invoice(s) in {args.days} days, {len(prices)} item(s) priced, {len(accounts)} account(s) on file.", rows,
                   f"history for {args.days} days")  # fmt: skip


class SimilarArgs(BaseModel):
    reason_code: str = Field(max_length=60)
    limit: int = Field(default=5, ge=1, le=10)


@registry.tool(
    "similar_exceptions", "Resolved exceptions with the same reason or finding code: how people decided them."
)
async def similar_exceptions(ctx: ToolContext, args: SimilarArgs) -> ToolResult:
    async with tenant_session(ctx.org_id) as db:
        rows = _rows(await db.execute(text("""
            SELECT e.resolution, e.note, e.resolved_at, i.invoice_number FROM exceptions e JOIN invoices i ON i.id = e.invoice_id
            WHERE e.status = 'resolved' AND (e.reasons @> CAST(:c AS jsonb) OR e.findings @> CAST(:c AS jsonb))
            ORDER BY e.resolved_at DESC LIMIT :n"""), {"c": json.dumps([{"code": args.reason_code}]), "n": args.limit}))  # fmt: skip
    return _result(ctx, f"{len(rows)} resolved exception(s) with {args.reason_code}." if rows else f"No resolved exception with {args.reason_code} yet.", rows,
                   f"similar to {args.reason_code}")  # fmt: skip


PART_CHARS = 3000  # a part's JSON encoding stays well inside the agent's per-result limit (agent/context.py)


def page_parts(text_of_page: str, limit: int = PART_CHARS) -> list[str]:
    """A page in consecutive parts, each small enough once JSON-encoded to be shown to the agent in full."""
    parts, start = [], 0
    while True:
        end = min(len(text_of_page), start + limit)
        while end - start > 1 and len(json.dumps(text_of_page[start:end])) > limit:
            size = end - start
            end = start + max(1, size * limit // len(json.dumps(text_of_page[start:end])))
        parts.append(text_of_page[start:end])
        start = end
        if start >= len(text_of_page):
            return parts


class PageArgs(BaseModel):
    invoice_id: UUID
    page: int = Field(default=1, ge=1, le=200)
    part: int = Field(
        default=1, ge=1, le=100, description="Long pages come in parts: 1 first, then the next the summary names."
    )


@registry.tool(
    "document_text",
    "The text of a page of the invoice's document, as read from the file. A long page comes in parts: the summary says "
    "how many there are; ask for the next part until you have them all.",
)
async def document_text(ctx: ToolContext, args: PageArgs) -> ToolResult:
    async with tenant_session(ctx.org_id) as db:
        row = (await db.execute(text("SELECT p.text FROM document_pages p JOIN invoices i ON i.document_id = p.document_id WHERE i.id = :i AND p.page = :p"),
                                {"i": args.invoice_id, "p": args.page})).first()  # fmt: skip
    if row is None:
        raise ToolError("No such page for this invoice (it may have been entered without a document).")
    parts = page_parts(row.text or "")
    if args.part > len(parts):
        raise ToolError(f"Page {args.page} has {len(parts)} part(s); ask for a part from 1 to {len(parts)}.")
    more = f" Ask for part {args.part + 1} for the rest." if args.part < len(parts) else ""
    return _result(
        ctx,
        f"Page {args.page}, part {args.part} of {len(parts)}.{more}",
        [{"page": args.page, "part": args.part, "parts": len(parts), "text": parts[args.part - 1]}],
        f"page {args.page}, part {args.part}, of invoice {args.invoice_id}",
    )


@registry.tool(
    "compare_lines",
    "Line by line: ordered, received, already invoiced, invoiced now, and the price difference, computed.",
)
async def compare_lines(ctx: ToolContext, args: InvoiceArgs) -> ToolResult:
    from app.matching import tolerances
    from app.matching.match import match_invoice

    async with tenant_session(ctx.org_id) as db:
        if await db.scalar(text("SELECT 1 FROM invoices WHERE id = :i"), {"i": args.invoice_id}) is None:
            raise ToolError("No invoice with this id in this organisation.")
        result = await match_invoice(db, args.invoice_id)  # read only: nothing is stored
        tolerance = float((await tolerances.load(db)).price_tolerance_pct)
    rows = []
    for ln in result.lines:
        quantity, price, po_price = ln["_quantity"], ln["_price"], Decimal(ln["po_unit_price"])
        difference = None if price is None else ((price - po_price) * quantity).quantize(Decimal("0.01"))
        rows.append({"line": ln["line_no"], "item_code": ln["item_code"], "ordered": float(Decimal(ln["ordered"])), "received": float(Decimal(ln["received"])),
                     "previously_invoiced": float(Decimal(ln["previously_invoiced"])), "invoiced_now": float(quantity), "po_unit_price": float(po_price),
                     "invoice_unit_price": None if price is None else float(price), "price_variance_pct": None if ln["_pct"] is None else float(ln["_pct"]),
                     "price_difference_total": None if difference is None else float(difference),
                     "price_tolerance_pct": tolerance})  # fmt: skip
    rows += [{"section": "reason", "code": r.code, "detail": r.detail, "line": r.line_no} for r in result.reasons]
    return _result(ctx, f"{result.status}: " + ("; ".join(r.code for r in result.reasons) or "no problems"), rows, "line comparison",
                   extra={"reason_codes": [r.code for r in result.reasons]})  # fmt: skip


# Findings that a price variance cannot explain away: a person decides these, never an accepted variance.
VARIANCE_HELD = {"bank_changed", "bank_unverified", "duplicate_number", "duplicate_amount"}


class RecommendArgs(BaseModel):
    exception_id: UUID
    outcome: OUTCOMES
    rationale: str = Field(max_length=1500)
    evidence: list[str] = Field(min_length=1, max_length=10, description="Evidence handles that support it")


@registry.tool(
    "recommend",
    "Recommend one outcome for the exception, with the evidence. A person accepts or changes it; nothing else happens.",
)
async def recommend(ctx: ToolContext, args: RecommendArgs) -> ToolResult:
    async with tenant_session(ctx.org_id) as db:
        bound = await db.scalar(text("SELECT exception_id FROM runs WHERE id = :r"), {"r": ctx.run_id})
        findings = await db.scalar(text("SELECT findings FROM exceptions WHERE id = :e"), {"e": args.exception_id})
    # A run recommends on the one exception it was started for, and only that; a free question does not recommend.
    if bound is None:
        raise ToolError(
            "This run was not started for an exception, so it cannot recommend. Answer the question with what you found."
        )
    if bound != args.exception_id:
        raise ToolError(
            "This run was started for a different exception; it may only recommend on the exception it was started for."
        )
    if args.outcome == "approve_with_variance" and (held := {f["code"] for f in findings or []} & VARIANCE_HELD):
        raise ToolError(
            f"Do not recommend approve with variance: this exception has {', '.join(sorted(held))}, which a person must decide."
        )
    known = await _evidence(ctx, args.evidence)
    if missing := [h for h in args.evidence if h not in known]:
        raise ToolError(f"Unknown evidence handle(s): {', '.join(missing)}.")
    async with tenant_session(ctx.org_id) as db:
        status = await db.scalar(text("SELECT status FROM exceptions WHERE id = :e"), {"e": args.exception_id})
    if status is None:
        raise ToolError("No exception with this id in this organisation.")
    if status == "resolved":
        raise ToolError("This exception is already resolved.")
    recommendation = {
        "outcome": args.outcome,
        "rationale": args.rationale,
        "evidence": args.evidence,
        "run_id": str(ctx.run_id),
    }

    async def commit(db) -> None:
        await db.execute(text("UPDATE exceptions SET recommendation = CAST(:r AS jsonb), status = CASE WHEN status = 'escalated' THEN status ELSE 'recommended' END WHERE id = :e"),
                         {"r": json.dumps(recommendation), "e": args.exception_id})  # fmt: skip

    return ToolResult(f"Recommendation stored: {args.outcome}. A person will decide.", commit=commit)


class DraftArgs(BaseModel):
    exception_id: UUID
    kind: Literal["credit_note_request", "corrected_invoice_request"]
    body: str = Field(max_length=3000)


@registry.tool(
    "draft_supplier_message", "Store a draft message to the supplier for a person to review and send. Nothing is sent."
)
async def draft_supplier_message(ctx: ToolContext, args: DraftArgs) -> ToolResult:
    async with tenant_session(ctx.org_id) as db:
        if await db.scalar(text("SELECT 1 FROM exceptions WHERE id = :e"), {"e": args.exception_id}) is None:
            raise ToolError("No exception with this id in this organisation.")

    async def commit(db) -> None:
        await db.execute(text("INSERT INTO drafts (org_id, exception_id, run_id, kind, body) VALUES (:o, :e, :r, :k, :b)"),
                         {"o": ctx.org_id, "e": args.exception_id, "r": ctx.run_id, "k": args.kind, "b": args.body})  # fmt: skip

    return ToolResult("Draft stored for a person to send.", commit=commit)
