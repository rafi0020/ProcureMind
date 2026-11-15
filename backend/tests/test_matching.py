"""The three-way match: invoice lines against the PO and what was received, within the organisation's tolerances."""

from datetime import date
from decimal import Decimal as D

from sqlalchemy import select

from app.db import tenant_session
from app.models.ap import Invoice, InvoiceLine
from tests.conftest import rows


async def setup_po(team, *, ordered="10", received="10", price="4.50", currency="USD", po_number="PO-00123"):
    vendor = (
        await team.admin.post("/api/vendors", json={"name": f"Acme {po_number}", "default_currency": "USD"})
    ).json()["id"]
    po = (await team.clerk.post("/api/orders", json={"vendor_id": vendor, "po_number": po_number, "currency": currency,
                                                      "lines": [{"item_code": "W-1", "description": "Widget, steel", "quantity": ordered, "unit_price": price}]})).json()  # fmt: skip
    if received != "0":
        r = await team.clerk.post(
            f"/api/orders/{po['id']}/receipts",
            json={
                "receipt_no": f"GRN-{po_number}",
                "lines": [{"po_line_id": po["lines"][0]["id"], "quantity": received}],
            },
        )
        assert r.status_code == 201, r.text
    return vendor, po


async def make_invoice(
    team,
    vendor,
    *,
    number,
    qty,
    price,
    po="po123",
    currency="USD",
    kind="invoice",
    item="W-1",
    description="Widget, steel",
) -> str:
    amount = (D(qty) * D(price)).quantize(D("0.01"))
    async with tenant_session(team.org_id) as db:
        inv = Invoice(org_id=team.org_id, vendor_id=vendor, kind=kind, invoice_number=number, invoice_number_norm=number.lower(), invoice_date=date(2026, 10, 5),
                      po_number=po, currency=currency, subtotal=amount, tax=D("0.00"), total=amount, status="ready", entered_by=team.user_ids["clerk"])  # fmt: skip
        db.add(inv)
        await db.flush()
        db.add(
            InvoiceLine(
                org_id=team.org_id,
                invoice_id=inv.id,
                line_no=1,
                item_code=item,
                description=description,
                quantity=D(qty),
                unit_price=D(price),
                amount=amount,
            )
        )
        return str(inv.id)


async def match(team, invoice_id, **body):
    r = await team.clerk.post(f"/api/invoices/{invoice_id}/match", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def codes(result):
    return [r["code"] for r in result["reasons"]]


async def status_of(team, invoice_id):
    return (await rows(team.org_id, "SELECT status FROM invoices WHERE id = :i", i=invoice_id))[0][0]


async def test_an_exact_invoice_matches_and_goes_to_approval(team):
    vendor, _ = await setup_po(team)
    inv = await make_invoice(team, vendor, number="A1", qty="10", price="4.50")  # PO "PO-00123" found from "po123"
    result = await match(team, inv)
    assert (
        result["status"] == "matched" and result["reasons"] == [] and await status_of(team, inv) == "pending_approval"
    )
    line = result["lines"][0]
    assert (line["ordered"], line["received"], line["previously_invoiced"], line["invoiced"]) == ("10", "10", "0", "10")


async def test_price_within_tolerance_is_a_variance_and_beyond_it_an_exception(team):
    vendor, _ = await setup_po(team, ordered="20", received="20")
    within = await match(team, await make_invoice(team, vendor, number="A1", qty="5", price="4.5675"))  # +1.5%
    assert within["status"] == "matched_with_variance" and within["lines"][0]["price_variance_pct"] == "1.50"
    beyond = await match(team, await make_invoice(team, vendor, number="A2", qty="5", price="4.635"))  # +3%
    assert beyond["status"] == "exception" and codes(beyond) == ["price_over_tolerance"]


async def test_two_invoices_cannot_bill_more_than_was_received(team):
    """Review Focus 2: 10 ordered and received; two invoices of 6 each."""
    vendor, _ = await setup_po(team)
    first = await match(team, await make_invoice(team, vendor, number="A1", qty="6", price="4.50"))
    second_id = await make_invoice(team, vendor, number="A2", qty="6", price="4.50")
    second = await match(team, second_id)
    assert first["status"] == "matched"
    assert (
        second["status"] == "exception"
        and codes(second) == ["quantity_over_received"]
        and await status_of(team, second_id) == "exception"
    )
    assert second["lines"][0]["previously_invoiced"] == "6"
    stored = (await team.viewer.get(f"/api/invoices/{second_id}/match")).json()
    assert (stored["lines"][0]["received"], stored["lines"][0]["previously_invoiced"]) == ("10", "6")


async def test_invoicing_more_than_was_received_is_an_exception(team):
    vendor, _ = await setup_po(team, ordered="10", received="5")
    assert codes(await match(team, await make_invoice(team, vendor, number="A1", qty="8", price="4.50"))) == [
        "quantity_over_received"
    ]


async def test_a_currency_mismatch_is_reported_without_comparing_amounts(team):
    """Review Focus 4."""
    vendor, _ = await setup_po(team)
    result = await match(team, await make_invoice(team, vendor, number="A1", qty="10", price="4.50", currency="EUR"))
    assert result["status"] == "exception" and codes(result) == ["currency_mismatch"] and result["lines"] == []
    assert "4.5" not in str(result["reasons"])


async def test_a_credit_note_reduces_what_was_invoiced_and_is_never_approved_for_payment(team):
    """Review Focus 3."""
    vendor, _ = await setup_po(team)
    assert (await match(team, await make_invoice(team, vendor, number="A1", qty="10", price="4.50")))[
        "status"
    ] == "matched"
    credit = await make_invoice(team, vendor, number="CN1", qty="1", price="4.50", kind="credit_note")
    assert (await match(team, credit))["status"] == "matched" and await status_of(team, credit) == "matched"
    later = await match(team, await make_invoice(team, vendor, number="A2", qty="1", price="4.50"))
    assert later["status"] == "matched" and later["lines"][0]["previously_invoiced"] == "9"


async def test_no_po_wrong_vendor_and_unmatched_lines_are_exceptions(team):
    vendor, _ = await setup_po(team)
    other = (await team.admin.post("/api/vendors", json={"name": "Someone Else", "default_currency": "USD"})).json()[
        "id"
    ]
    assert codes(
        await match(team, await make_invoice(team, vendor, number="A1", qty="1", price="4.50", po="PO-99999"))
    ) == ["no_po"]
    assert codes(await match(team, await make_invoice(team, other, number="A2", qty="1", price="4.50"))) == [
        "vendor_mismatch"
    ]
    odd = await match(
        team,
        await make_invoice(
            team, vendor, number="A3", qty="1", price="4.50", item="Z-9", description="Something unrelated"
        ),
    )
    assert codes(odd) == ["line_unmatched"]
    by_description = await match(
        team,
        await make_invoice(team, vendor, number="A4", qty="1", price="4.50", item=None, description="Steel widget"),
    )
    assert by_description["status"] == "matched"


async def test_a_clerk_can_match_to_another_po_by_hand_and_it_is_audited(team):
    vendor, po = await setup_po(team)
    inv = await make_invoice(team, vendor, number="A1", qty="10", price="4.50", po=None)
    assert codes(await match(team, inv)) == ["no_po"]
    manual = await match(team, inv, po_id=po["id"])
    assert manual["status"] == "matched" and manual["manual"] is True
    assert (await team.admin.get("/api/audit", params={"action": "invoice.matched_by_hand"})).json()["entries"]
    assert (await team.viewer.post(f"/api/invoices/{inv}/match", json={})).status_code == 403


async def test_tolerances_are_an_admin_setting(team):
    vendor, _ = await setup_po(team, ordered="20", received="20")
    assert (await team.clerk.put("/api/ap-settings", json={"price_tolerance_pct": "5"})).status_code == 403
    assert (await team.admin.put("/api/ap-settings", json={"price_tolerance_pct": "5"})).json()[
        "price_tolerance_pct"
    ] == "5.00"
    assert (await match(team, await make_invoice(team, vendor, number="A1", qty="5", price="4.635")))[
        "status"
    ] == "matched_with_variance"


async def test_an_invoice_from_a_document_is_matched_by_the_pipeline(team):
    from sqlalchemy import text

    from app.db import system_session
    from app.intake.queue import process_pending
    from seed.documents import render_pdf
    from tests.samples import invoice

    vendor = (
        await team.admin.post("/api/vendors", json={"name": "Acme Supplies Ltd", "default_currency": "USD"})
    ).json()["id"]
    acc = (
        await team.admin.post(
            f"/api/vendors/{vendor}/bank-accounts",
            json={"iban_or_account": "GB33BUKB20201555555555", "bank_name": "Demo"},
        )
    ).json()
    await team.admin.post(
        f"/api/vendors/{vendor}/bank-accounts/{acc['id']}/decide", json={"decision": "approved"}
    )  # the account the document prints (I7)
    await team.clerk.post("/api/orders", json={"vendor_id": vendor, "po_number": "PO-00123", "currency": "USD", "lines": [
        {"item_code": "W-1", "description": "Widget, steel", "quantity": "10", "unit_price": "4.50"},
        {"item_code": "B-7", "description": "Bracket", "quantity": "4", "unit_price": "12.00"}]})  # fmt: skip
    po = (await team.clerk.get("/api/orders")).json()["orders"][0]
    detail = (await team.clerk.get(f"/api/orders/{po['id']}")).json()
    await team.clerk.post(
        f"/api/orders/{po['id']}/receipts",
        json={
            "receipt_no": "G1",
            "lines": [{"po_line_id": ln["id"], "quantity": ln["quantity"]} for ln in detail["lines"]],
        },
    )
    async with system_session() as db:
        await db.execute(text("UPDATE documents SET status = 'failed' WHERE status IN ('queued', 'processing')"))
    await team.clerk.post(
        "/api/documents", files=[("files", ("i.pdf", render_pdf(invoice(number="INV-8080")), "application/pdf"))]
    )
    await process_pending("test")
    status = (await rows(team.org_id, "SELECT status FROM invoices WHERE invoice_number = 'INV-8080'"))[0][0]
    assert status == "pending_approval"


async def header_only(team, vendor, *, number, total, po="PO-00123") -> str:
    inv = await make_invoice(team, vendor, number=number, qty="1", price=total, po=po)
    await rows(team.org_id, "DELETE FROM invoice_lines WHERE invoice_id = :i RETURNING 1", i=inv)
    return inv


async def test_an_invoice_without_lines_is_held_to_what_was_received_and_not_yet_billed(team):
    """No lines were read: the subtotal is compared with the value received less what other invoices billed."""
    vendor, _ = await setup_po(team, ordered="10", received="4", price="4.50")  # 45.00 ordered, 18.00 received
    over = await match(team, await header_only(team, vendor, number="H1", total="45.00"))
    assert codes(over) == ["total_over_received"] and "18.00" in over["reasons"][0]["detail"]
    first = await match(team, await header_only(team, vendor, number="H2", total="18.00"))
    assert first["status"] == "matched"
    again = await match(team, await header_only(team, vendor, number="H3", total="18.00"))
    assert codes(again) == ["total_over_received"] and "18.00 already invoiced" in again["reasons"][0]["detail"]


async def set_header(team, invoice_id, **values):
    """Set the printed header values directly, as an extraction that read them differently would have."""
    async with tenant_session(team.org_id) as db:
        inv = await db.get(Invoice, invoice_id)
        for key, value in values.items():
            setattr(inv, key, value)


async def test_an_invoice_whose_total_exceeds_its_matched_lines_is_an_exception(team):
    """C1: 10 x 4.50 on the lines, but the total says 9,000.00 and no subtotal was read."""
    vendor, _ = await setup_po(team)
    inv = await make_invoice(team, vendor, number="C1A", qty="10", price="4.50")
    await set_header(team, inv, subtotal=None, tax=None, total=D("9000.00"))
    result = await match(team, inv)
    assert result["status"] == "exception" and codes(result) == ["total_not_matched"]
    assert await status_of(team, inv) == "exception"
    assert "9000.00" in result["reasons"][0]["detail"] and "45.00" in result["reasons"][0]["detail"]


async def test_a_line_without_a_unit_price_is_an_exception(team):
    """C1 variant: the line's price is not read, so its value cannot be checked against the order."""
    vendor, _ = await setup_po(team)
    inv = await make_invoice(team, vendor, number="C1B", qty="10", price="4.50")
    async with tenant_session(team.org_id) as db:
        line = (await db.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == inv))).scalar_one()
        line.unit_price = None
    result = await match(team, inv)
    assert result["status"] == "exception" and codes(result) == ["line_unpriced"]
    assert await status_of(team, inv) == "exception"


async def test_a_lineless_invoice_without_a_subtotal_is_held_to_its_total(team):
    """C1 variant: no lines and no subtotal, so the total is the value checked against what was received."""
    vendor, _ = await setup_po(team, ordered="10", received="10", price="4.50")
    inv = await header_only(team, vendor, number="C1C", total="9000.00")
    await set_header(team, inv, subtotal=None)
    result = await match(team, inv)
    assert result["status"] == "exception" and codes(result) == ["total_over_received"]


async def test_two_invoices_matched_at_once_cannot_both_bill_the_same_receipt(team):
    """C3(a): 10 received; two invoices of 6 and 5 matched at the same moment."""
    import asyncio

    vendor, _ = await setup_po(team)
    six = await make_invoice(team, vendor, number="R1", qty="6", price="4.50")
    five = await make_invoice(team, vendor, number="R2", qty="5", price="4.50")
    first, second = await asyncio.gather(
        team.clerk.post(f"/api/invoices/{six}/match", json={}), team.clerk.post(f"/api/invoices/{five}/match", json={})
    )
    assert first.status_code == 200 and second.status_code == 200
    assert [c for r in (first, second) for c in codes(r.json())].count("quantity_over_received") == 1


async def test_two_lines_on_one_po_line_are_added_together(team):
    """C3(b): one invoice, two lines of 6 on the same PO line of 10 received."""
    vendor, _ = await setup_po(team, ordered="10", received="10")
    inv = await make_invoice(team, vendor, number="C3B", qty="6", price="4.50")
    async with tenant_session(team.org_id) as db:
        db.add(
            InvoiceLine(
                org_id=team.org_id,
                invoice_id=inv,
                line_no=2,
                item_code="W-1",
                description="Widget, steel",
                quantity=D("6"),
                unit_price=D("4.50"),
                amount=D("27.00"),
            )
        )
    await set_header(team, inv, subtotal=D("54.00"), tax=D("0.00"), total=D("54.00"))
    result = await match(team, inv)
    assert result["status"] == "exception" and "quantity_over_received" in codes(result)
    assert await status_of(team, inv) == "exception"


async def test_a_lined_invoice_sees_what_a_header_only_invoice_already_billed(team):
    """C3(c): a header-only invoice for all 10 units is matched; a lined invoice for 9 of the same units must not be."""
    vendor, _ = await setup_po(team, ordered="10", received="10", price="4.50")
    assert (await match(team, await header_only(team, vendor, number="C3C1", total="45.00")))["status"] == "matched"
    nine = await make_invoice(team, vendor, number="C3C2", qty="9", price="4.50")
    result = await match(team, nine)
    assert result["status"] == "exception" and "total_over_received" in codes(result)
    assert await status_of(team, nine) == "exception"


async def test_an_invoice_cannot_be_given_another_organisations_vendor(team, other_team):
    """I9: a vendor the caller cannot see is not found; neither is an id that does not exist."""
    vendor, _ = await setup_po(team)
    foreign = (
        await other_team.admin.post("/api/vendors", json={"name": "Foreign Co", "default_currency": "USD"})
    ).json()["id"]
    inv = await make_invoice(team, vendor, number="I9", qty="10", price="4.50")
    assert (await team.clerk.patch(f"/api/invoices/{inv}/fields", json={"vendor_id": foreign})).status_code == 404
    unknown = "00000000-0000-0000-0000-000000000001"
    assert (await team.clerk.patch(f"/api/invoices/{inv}/fields", json={"vendor_id": unknown})).status_code == 404
    assert (await rows(team.org_id, "SELECT vendor_id FROM invoices WHERE id = :i", i=inv))[0][0] is not None


async def test_a_negative_quantity_on_an_invoice_line_is_an_exception(team):
    """Found in review: a negative line quantity passed and reduced what was billed. A credit is a credit note."""
    vendor, _ = await setup_po(team)
    inv = await make_invoice(team, vendor, number="NEG1", qty="-4", price="4.50")
    result = await match(team, inv)
    assert result["status"] == "exception" and codes(result) == ["quantity_negative"], result
    assert await status_of(team, inv) == "exception"


async def test_tolerance_and_tier_changes_are_stored_as_numbers(team):
    """Found in review: approval tier limits were saved as strings."""
    r = await team.admin.put(
        "/api/ap-settings",
        json={"approval_tiers": [
            {"up_to": "2500.00", "approvals": 1, "role": "approver"},
            {"up_to": None, "approvals": 2, "role": "admin"},
        ]},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    stored = (await rows(team.org_id, "SELECT approval_tiers FROM org_ap_settings"))[0][0]
    assert isinstance(stored[0]["up_to"], (int, float)) and stored[0]["up_to"] == 2500
    assert stored[1]["up_to"] is None
