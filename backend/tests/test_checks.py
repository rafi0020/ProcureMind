"""Duplicates and anomalies: scored checks with reasons. A finding or a failed match opens one exception."""

from datetime import date, timedelta
from decimal import Decimal as D

from app.checks.run import run_checks
from app.db import tenant_session
from tests.conftest import rows
from tests.test_matching import make_invoice, setup_po


async def findings(team, invoice_id) -> dict:
    async with tenant_session(team.org_id) as db:
        return {f.code: f for f in await run_checks(db, invoice_id)}


async def approved_account(team, vendor_id, account):
    """An account on file for the vendor, approved as the bank-change process requires. I7: a printed account is
    checked against it, so the documents' account must be on file."""
    acc = (
        await team.admin.post(
            f"/api/vendors/{vendor_id}/bank-accounts", json={"iban_or_account": account, "bank_name": "Demo"}
        )
    ).json()
    await team.admin.post(f"/api/vendors/{vendor_id}/bank-accounts/{acc['id']}/decide", json={"decision": "approved"})


async def status_of(team, invoice_id):
    return (await rows(team.org_id, "SELECT status FROM invoices WHERE id = :i", i=invoice_id))[0][0]


async def test_the_same_file_twice_is_one_invoice_and_a_renumbered_copy_is_caught(team):
    """Review Focus 1."""
    from sqlalchemy import text

    from app.db import system_session
    from app.intake.queue import process_pending
    from seed.documents import render_pdf
    from tests.samples import invoice

    vendor, po = await setup_po(team, ordered="100", received="100", po_number="PO-00123")
    await team.admin.patch(f"/api/vendors/{vendor}", json={"name": "Acme Supplies Ltd"})
    await approved_account(team, vendor, "GB33BUKB20201555555555")  # the account the documents print
    async with system_session() as db:
        await db.execute(text("UPDATE documents SET status = 'failed' WHERE status IN ('queued', 'processing')"))
    lines = [__import__("seed.documents", fromlist=["Line"]).Line("W-1", "Widget, steel", D(10), D("4.50"))]
    first = render_pdf(invoice(number="INV-0012", lines=lines))
    for _ in range(2):
        await team.clerk.post("/api/documents", files=[("files", ("a.pdf", first, "application/pdf"))])
    await process_pending("test")
    assert (await rows(team.org_id, "SELECT count(*) FROM invoices"))[0][0] == 1
    assert (await rows(team.org_id, "SELECT status FROM invoices"))[0][0] == "pending_approval"

    await team.clerk.post(
        "/api/documents",
        files=[("files", ("b.pdf", render_pdf(invoice(number="inv 12", lines=lines, layout=1)), "application/pdf"))],
    )
    await process_pending("test")
    second = (await rows(team.org_id, "SELECT id, status FROM invoices WHERE invoice_number = 'inv 12'"))[0]
    assert second.status == "exception"
    exc = (await team.approver.get("/api/exceptions")).json()["exceptions"]
    assert [f["code"] for f in exc[0]["findings"]] == ["duplicate_number"] and exc[0]["invoice_id"] == str(second.id)


async def test_a_bank_account_that_differs_from_the_vendors_is_flagged(team):
    vendor, _ = await setup_po(team)
    acc = (
        await team.admin.post(
            f"/api/vendors/{vendor}/bank-accounts", json={"iban_or_account": "GB33BUKB20201555555555", "bank_name": "A"}
        )
    ).json()
    await team.admin.post(f"/api/vendors/{vendor}/bank-accounts/{acc['id']}/decide", json={"decision": "approved"})
    inv = await make_invoice(team, vendor, number="B1", qty="1", price="4.50")
    await rows(
        team.org_id, "UPDATE invoices SET bank_account = 'DE89 3704 0044 0532 0130 00' WHERE id = :i RETURNING 1", i=inv
    )
    assert (await findings(team, inv))["bank_changed"].score == 0.9
    await rows(
        team.org_id, "UPDATE invoices SET bank_account = 'GB33 BUKB 2020 1555 5555 55' WHERE id = :i RETURNING 1", i=inv
    )
    assert "bank_changed" not in await findings(team, inv)


async def test_invoices_split_to_stay_under_an_approval_limit_are_flagged(team):
    vendor, _ = await setup_po(team, ordered="100", received="100", price="950")
    ids = [await make_invoice(team, vendor, number=f"S{n}", qty="1", price="950") for n in range(3)]
    for n, i in enumerate(ids):
        await rows(
            team.org_id,
            "UPDATE invoices SET invoice_date = :d WHERE id = :i RETURNING 1",
            d=date(2026, 10, 1) + timedelta(days=2 * n),
            i=i,
        )
    found = await findings(team, ids[2])
    assert found["split_under_threshold"].score == 0.8 and "1000" in found["split_under_threshold"].reason


async def test_a_price_far_above_the_vendors_history_is_flagged(team):
    vendor, _ = await setup_po(team, ordered="100", received="100")
    old = await make_invoice(team, vendor, number="H1", qty="1", price="4.50")
    await rows(
        team.org_id,
        "UPDATE invoices SET status = 'approved', invoice_date = '2026-08-01' WHERE id = :i RETURNING 1",
        i=old,
    )
    new = await make_invoice(team, vendor, number="H2", qty="1", price="5.70")  # 27% above
    assert (await findings(team, new))["price_drift"].score == 0.6


async def test_a_same_amount_from_the_same_vendor_within_30_days_is_flagged(team):
    vendor, _ = await setup_po(team, ordered="100", received="100")
    await make_invoice(team, vendor, number="D1", qty="2", price="4.50")
    second = await make_invoice(team, vendor, number="D2", qty="2", price="4.50")
    assert (await findings(team, second))["duplicate_amount"].score == 0.7


async def test_a_clean_invoice_opens_nothing_and_resolving_is_for_approvers(team):
    vendor, _ = await setup_po(team)
    clean = await make_invoice(team, vendor, number="C1", qty="10", price="4.50")
    await team.clerk.post(f"/api/invoices/{clean}/match", json={})
    assert await findings(team, clean) == {} and await status_of(team, clean) == "pending_approval"
    assert (await team.approver.get("/api/exceptions")).json()["exceptions"] == []

    dup = await make_invoice(team, vendor, number="C1", qty="1", price="4.50")
    await team.clerk.post(f"/api/invoices/{dup}/match", json={})
    exc = (await team.approver.get("/api/exceptions")).json()["exceptions"][0]
    assert (
        await team.clerk.post(f"/api/exceptions/{exc['id']}/resolve", json={"outcome": "reject_duplicate"})
    ).status_code == 403
    done = await team.approver.post(
        f"/api/exceptions/{exc['id']}/resolve", json={"outcome": "reject_duplicate", "note": "Same as C1"}
    )
    assert done.json()["status"] == "resolved" and await status_of(team, dup) == "rejected"
    again = await team.approver.post(f"/api/exceptions/{exc['id']}/resolve", json={"outcome": "hold"})
    assert again.status_code == 409


async def test_approving_with_variance_sends_the_invoice_to_approval(team):
    vendor, _ = await setup_po(team, ordered="20", received="20")
    inv = await make_invoice(team, vendor, number="V1", qty="5", price="4.635")  # +3%, beyond tolerance
    await team.clerk.post(f"/api/invoices/{inv}/match", json={})
    exc = (await team.approver.get("/api/exceptions")).json()["exceptions"][0]
    assert [r["code"] for r in exc["reasons"]] == ["price_over_tolerance"]
    await team.approver.post(
        f"/api/exceptions/{exc['id']}/resolve", json={"outcome": "approve_with_variance", "note": "Agreed surcharge"}
    )
    assert await status_of(team, inv) == "pending_approval"


async def test_other_organisations_see_no_exceptions(team, other_team):
    vendor, _ = await setup_po(team)
    inv = await make_invoice(team, vendor, number="X1", qty="1", price="4.50", po="PO-404")
    await team.clerk.post(f"/api/invoices/{inv}/match", json={})
    exc = (await team.approver.get("/api/exceptions")).json()["exceptions"][0]
    assert (await other_team.admin.get(f"/api/exceptions/{exc['id']}")).status_code == 404
    assert (await other_team.admin.get("/api/exceptions")).json()["exceptions"] == []


async def test_an_invoice_whose_checks_failed_is_not_waiting_for_approval(team, monkeypatch):
    """I2: the match and the checks are one step. If the checks fail, nothing is left waiting unchecked."""
    from sqlalchemy import text

    from app.checks import run as checks
    from app.db import system_session
    from app.intake.queue import process_pending
    from seed.documents import Line, render_pdf
    from tests.samples import invoice

    vendor, _ = await setup_po(team, ordered="100", received="100", po_number="PO-00123")
    await team.admin.patch(f"/api/vendors/{vendor}", json={"name": "Acme Supplies Ltd"})
    await approved_account(team, vendor, "GB33BUKB20201555555555")  # the account the documents print
    async with system_session() as db:
        await db.execute(text("UPDATE documents SET status = 'failed' WHERE status IN ('queued', 'processing')"))
    lines = [Line("W-1", "Widget, steel", D(10), D("4.50"))]
    await team.clerk.post(
        "/api/documents",
        files=[("files", ("c.pdf", render_pdf(invoice(number="INV-0099", lines=lines)), "application/pdf"))],
    )

    async def checks_unavailable(db, invoice_id):
        raise RuntimeError("checks unavailable")

    monkeypatch.setattr(checks, "run_checks", checks_unavailable)
    await process_pending("test")
    status = (await rows(team.org_id, "SELECT status FROM invoices WHERE invoice_number = 'INV-0099'"))[0][0]
    assert status != "pending_approval"


async def test_a_printed_account_for_a_vendor_with_no_account_on_file_is_a_finding(team):
    """I7: an invoice that asks for payment to an account nobody has approved for the vendor is held."""
    from tests.test_matching import match

    vendor, _ = await setup_po(team)
    inv = await make_invoice(team, vendor, number="B7", qty="1", price="4.50")
    await rows(
        team.org_id, "UPDATE invoices SET bank_account = 'GB33 BUKB 2020 1555 5555 55' WHERE id = :i RETURNING 1", i=inv
    )
    assert (await findings(team, inv))["bank_unverified"].score == 0.6
    await match(team, inv)
    assert await status_of(team, inv) == "exception"


async def test_an_account_misread_by_ocr_is_still_the_account_on_file_but_a_real_difference_is_not(team):
    """Found by the scanned-invoice journey: OCR read GB00... as GBOO..., and the finding said both accounts end 0001."""
    vendor, _ = await setup_po(team, po_number="PO-OCR1")
    acc = (
        await team.admin.post(
            f"/api/vendors/{vendor}/bank-accounts", json={"iban_or_account": "GB00E2EB00000000000001", "bank_name": "A"}
        )
    ).json()
    await team.admin.post(f"/api/vendors/{vendor}/bank-accounts/{acc['id']}/decide", json={"decision": "approved"})
    inv = await make_invoice(team, vendor, number="B2", qty="1", price="4.50", po="PO-OCR1")
    await rows(
        team.org_id, "UPDATE invoices SET bank_account = 'GBOOE2EB0OOOOOOOOOOOO1' WHERE id = :i RETURNING 1", i=inv
    )
    assert "bank_changed" not in await findings(team, inv)
    await rows(
        team.org_id, "UPDATE invoices SET bank_account = 'GB00E2EB00000000000901' WHERE id = :i RETURNING 1", i=inv
    )
    changed = (await findings(team, inv))["bank_changed"]
    assert "ending 0901" in changed.reason and "ends 0001" in changed.reason
