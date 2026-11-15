"""AP records: extracted documents become invoices, POs and receipts; people correct what extraction missed."""

from decimal import Decimal as D

from sqlalchemy import text

from app.db import system_session
from app.intake.queue import process_pending
from seed.documents import Line, render_pdf
from tests.conftest import rows
from tests.samples import invoice


async def fresh_queue():
    async with system_session() as db:
        await db.execute(text("UPDATE documents SET status = 'failed' WHERE status IN ('queued', 'processing')"))


async def vendor(team, name="Acme Supplies Ltd"):
    r = await team.admin.post("/api/vendors", json={"name": name, "default_currency": "USD"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def ingest(team, spec) -> dict:
    await fresh_queue()
    r = await team.clerk.post(
        "/api/documents", files=[("files", (f"{spec.number or 'x'}.pdf", render_pdf(spec), "application/pdf"))]
    )
    assert r.status_code == 201, r.text
    await process_pending("test")
    listed = (await team.clerk.get("/api/invoices")).json()["invoices"]
    return next(i for i in listed if i["document_id"] == r.json()["documents"][0]["id"])


async def test_an_extracted_invoice_becomes_an_invoice_with_where_each_field_was_found(team):
    vendor_id = await vendor(team)
    inv = await ingest(team, invoice(number="INV-2001"))
    # Complete and arithmetically right, so it went on to matching; there is no purchase order PO-00123 here.
    assert inv["status"] == "exception" and inv["kind"] == "invoice" and inv["vendor_id"] == vendor_id
    detail = (await team.viewer.get(f"/api/invoices/{inv['id']}")).json()
    assert (detail["invoice_number"], detail["po_number"], detail["currency"], detail["total"]) == (
        "INV-2001",
        "PO-00123",
        "USD",
        "102.30",
    )
    assert detail["invoice_number_norm"] == "2001"
    assert [(ln["item_code"], ln["quantity"], ln["unit_price"], ln["amount"]) for ln in detail["lines"]] == [
        ("W-1", "10", "4.50", "45.00"),
        ("B-7", "4", "12.00", "48.00"),
    ]
    prov = {p["field"]: p for p in detail["provenance"]}
    assert prov["total"]["page"] == 1 and prov["total"]["confidence"] == 0.7 and prov["total"]["bbox"][0] > 0
    page = await team.viewer.get(f"/api/documents/{inv['document_id']}/pages/1.png")
    assert page.status_code == 200 and page.content.startswith(b"\x89PNG")


async def test_an_unknown_vendor_or_a_missing_field_needs_review_and_a_correction_fixes_it(team):
    vendor_id = await vendor(team)
    inv = await ingest(team, invoice(number=""))
    assert inv["status"] == "needs_review"
    r = await team.clerk.patch(f"/api/invoices/{inv['id']}/fields", json={"invoice_number": "INV-77"})
    assert r.status_code == 200 and r.json()["status"] == "ready" and r.json()["invoice_number_norm"] == "77"
    assert r.json()["corrected_by"] == [team.user_ids["clerk"]]
    entry = (await team.admin.get("/api/audit", params={"action": "invoice.corrected"})).json()["entries"][0]
    assert (
        entry["detail"]["before"]["invoice_number"] is None and entry["detail"]["after"]["invoice_number"] == "INV-77"
    )

    other = await ingest(team, invoice(number="INV-3001", vendor_name="Nobody Known Ltd"))
    assert other["status"] == "needs_review" and other["vendor_id"] is None
    fixed = await team.clerk.patch(f"/api/invoices/{other['id']}/fields", json={"vendor_id": vendor_id})
    assert fixed.json()["status"] == "ready"


async def test_a_correction_that_breaks_the_arithmetic_stays_in_review(team):
    await vendor(team)
    inv = await ingest(team, invoice(number="INV-4001"))
    r = await team.clerk.patch(f"/api/invoices/{inv['id']}/fields", json={"total": "110.00"})
    assert r.json()["status"] == "needs_review" and any("total" in issue for issue in r.json()["issues"])
    assert (await rows(team.org_id, "SELECT count(*) FROM matches WHERE invoice_id = :i", i=inv["id"]))[0][
        0
    ] == 0  # the old match is gone
    back = await team.clerk.patch(f"/api/invoices/{inv['id']}/fields", json={"total": "102.30"})
    assert back.json()["status"] == "ready"
    assert (await team.viewer.patch(f"/api/invoices/{inv['id']}/fields", json={"total": "1"})).status_code == 403


async def test_a_credit_note_is_stored_as_one_with_positive_amounts(team):
    await vendor(team)
    cn = await ingest(team, invoice(kind="credit_note", number="CN-5"))
    assert cn["kind"] == "credit_note" and cn["total"] == "102.30"


async def test_purchase_orders_and_receipts_by_api_with_changes_recorded(team):
    vendor_id = await vendor(team)
    po = await team.clerk.post("/api/orders", json={"vendor_id": vendor_id, "po_number": "PO-00123", "currency": "USD",
                                                    "lines": [{"item_code": "W-1", "description": "Widget", "quantity": "10", "unit_price": "4.50"}]})  # fmt: skip
    assert po.status_code == 201, po.text
    po = po.json()
    line = po["lines"][0]["id"]
    grn = await team.clerk.post(
        f"/api/orders/{po['id']}/receipts",
        json={"receipt_no": "GRN-1", "lines": [{"po_line_id": line, "quantity": "6"}]},
    )
    assert grn.status_code == 201
    again = await team.clerk.post(
        f"/api/orders/{po['id']}/receipts",
        json={"receipt_no": "GRN-1", "lines": [{"po_line_id": line, "quantity": "1"}]},
    )
    assert again.status_code == 409  # the same receipt number is one receipt
    too_many = await team.clerk.post(
        f"/api/orders/{po['id']}/receipts",
        json={"receipt_no": "GRN-2", "lines": [{"po_line_id": line, "quantity": "5"}]},
    )
    assert too_many.status_code == 422  # 6 + 5 received of 10 ordered
    changed = await team.clerk.patch(f"/api/orders/{po['id']}", json={"lines": [{"id": line, "unit_price": "4.75"}]})
    assert changed.status_code == 200
    detail = (await team.viewer.get(f"/api/orders/{po['id']}")).json()
    assert detail["lines"][0]["received"] == "6" and detail["lines"][0]["unit_price"] == "4.75"
    assert detail["changes"][0]["before"]["lines"][0]["unit_price"] == "4.50"
    assert (
        await team.viewer.post(
            "/api/orders", json={"vendor_id": vendor_id, "po_number": "X", "currency": "USD", "lines": []}
        )
    ).status_code == 403


async def test_other_organisations_cannot_see_invoices_or_orders(team, other_team):
    await vendor(team)
    inv = await ingest(team, invoice(number="INV-6001"))
    assert (await other_team.admin.get(f"/api/invoices/{inv['id']}")).status_code == 404
    assert (await other_team.admin.get("/api/invoices")).json()["invoices"] == []
    assert (await rows(other_team.org_id, "SELECT count(*) FROM invoices"))[0][0] == 0


async def test_a_purchase_order_whose_lines_cannot_be_read_needs_review_and_creates_no_order(team):
    await vendor(team)
    await fresh_queue()
    spec = invoice(number="PO-7001", kind="purchase_order")
    spec.lines = []
    r = await team.clerk.post("/api/documents", files=[("files", ("po.pdf", render_pdf(spec), "application/pdf"))])
    await process_pending("test")
    doc = r.json()["documents"][0]["id"]
    assert (await team.clerk.get(f"/api/documents/{doc}")).json()["status"] == "needs_review"
    assert not [o for o in (await team.clerk.get("/api/orders")).json()["orders"] if o["po_number"] == "PO-7001"]


async def upload(team, spec, filename="doc.pdf") -> str:
    r = await team.clerk.post("/api/documents", files=[("files", (filename, render_pdf(spec), "application/pdf"))])
    assert r.status_code == 201, r.text
    return r.json()["documents"][0]["id"]


async def widget_order(team, vendor_id, *, ordered="10", po_number="PO-00123") -> dict:
    r = await team.clerk.post("/api/orders", json={"vendor_id": vendor_id, "po_number": po_number, "currency": "USD",
                                                    "lines": [{"item_code": "W-1", "description": "Widget, steel", "quantity": ordered, "unit_price": "4.50"}]})  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()


async def received_on_widgets(team) -> int:
    return int((await rows(team.org_id, "SELECT coalesce(sum(quantity), 0) FROM receipt_lines"))[0][0])


async def test_a_receipt_document_cannot_take_a_line_over_the_ordered_quantity(team):
    """C4: a goods receipt read from a document cannot receive more than was ordered."""
    await widget_order(team, await vendor(team))
    await fresh_queue()
    doc = await upload(
        team, invoice(number="GRN-9001", kind="goods_receipt", lines=[Line("W-1", "Widget, steel", D(30), D("4.50"))])
    )
    await process_pending("test")
    status, error = (await rows(team.org_id, "SELECT status, error FROM documents WHERE id = :d", d=doc))[0]
    assert status == "needs_review" and "ordered" in error
    assert await received_on_widgets(team) == 0


async def test_the_same_receipt_number_twice_is_one_receipt(team):
    """C4: two documents with the same receipt number on one order are one receipt."""
    await widget_order(team, await vendor(team))
    await fresh_queue()
    await upload(
        team, invoice(number="GRN-9002", kind="goods_receipt", lines=[Line("W-1", "Widget, steel", D(6), D("4.50"))])
    )
    second = await upload(
        team,
        invoice(
            number="GRN-9002",
            kind="goods_receipt",
            layout=1,
            note="copy",
            lines=[Line("W-1", "Widget, steel", D(6), D("4.50"))],
        ),
    )
    await process_pending("test")
    assert await received_on_widgets(team) == 6
    assert (await rows(team.org_id, "SELECT count(*) FROM goods_receipts"))[0][0] == 1
    assert (await rows(team.org_id, "SELECT status FROM documents WHERE id = :d", d=second))[0][0] == "needs_review"


async def test_a_receipt_or_order_arriving_by_inbox_waits_for_a_person(team):
    """C4: the inbox is not a person; orders and receipts it brings are held for one."""
    import base64

    import httpx

    key = (await team.admin.post("/api/inbox-keys", json={"name": "Suppliers"})).json()["key"]
    await vendor(team)
    await fresh_queue()
    docs = {
        "po": invoice(
            number="PO-7100", kind="purchase_order", po=None, lines=[Line("W-1", "Widget, steel", D(10), D("4.50"))]
        ),
        "grn": invoice(number="GRN-7100", kind="goods_receipt", lines=[Line("W-1", "Widget, steel", D(4), D("4.50"))]),
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=team.app), base_url="http://test") as c:
        for name, spec in docs.items():
            body = {"from": "supplier@example.test", "subject": name, "attachments": [
                {"filename": f"{name}.pdf", "content_base64": base64.b64encode(render_pdf(spec)).decode()}]}  # fmt: skip
            assert (await c.post("/v1/inbox", json=body, headers={"Authorization": f"Bearer {key}"})).status_code == 200
    await process_pending("test")
    rows_out = await rows(
        team.org_id, "SELECT status, error FROM documents WHERE source = 'inbox' ORDER BY received_at"
    )
    assert [r.status for r in rows_out] == ["needs_review", "needs_review"]
    assert all("confirmed by a person" in r.error for r in rows_out)
    assert (await rows(team.org_id, "SELECT count(*) FROM purchase_orders WHERE po_number = 'PO-7100'"))[0][0] == 0
    assert await received_on_widgets(team) == 0


async def test_a_purchase_order_whose_number_already_exists_needs_review(team):
    """Minor H, folded into C4: a second order with a number that already exists is a review item, not a crash.
    Two copies of one order document (different layouts, so different files) read to the same number."""
    await vendor(team)
    await fresh_queue()
    first = await upload(team, invoice(number="PO-00123", kind="purchase_order"))
    second = await upload(team, invoice(number="PO-00123", kind="purchase_order", layout=1, note="copy"))
    await process_pending("test")
    assert (await rows(team.org_id, "SELECT count(*) FROM purchase_orders"))[0][0] == 1
    status, error = (await rows(team.org_id, "SELECT status, error FROM documents WHERE id = :d", d=second))[0]
    assert status == "needs_review" and "already exists" in error
    assert (await rows(team.org_id, "SELECT status FROM documents WHERE id = :d", d=first))[0][0] == "extracted"


async def test_two_vendors_with_the_same_tax_id_send_the_invoice_to_review_without_a_crash(team):
    """Found in review: two vendors with one tax ID made the document fail with MultipleResultsFound."""
    for name in ("Acme Supplies Ltd", "Acme Supplies (Overseas) Ltd"):
        r = await team.admin.post(
            "/api/vendors", json={"name": name, "tax_id": "GB123456789", "default_currency": "USD"}
        )
        assert r.status_code == 201, r.text
    inv = await ingest(team, invoice(number="INV-7001"))
    assert inv["status"] == "needs_review"
    assert any("tax ID GB123456789" in issue and "belongs to 2 vendors" in issue for issue in inv["issues"]), inv[
        "issues"
    ]
