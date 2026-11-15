"""Approval chains by amount, with separation of duties enforced inside the transaction that records a decision."""

import asyncio

from tests.conftest import PASSWORD, rows
from tests.test_matching import make_invoice, match, setup_po


async def pending(team, *, price="45", qty="10", number="P1") -> str:
    vendor, _ = await setup_po(team, ordered=qty, received=qty, price=price, po_number=f"PO-{number}")
    inv = await make_invoice(team, vendor, number=number, qty=qty, price=price, po=f"PO-{number}")
    result = (await team.clerk.post(f"/api/invoices/{inv}/match", json={})).json()
    assert result["status"] == "matched", result
    assert (await rows(team.org_id, "SELECT status FROM invoices WHERE id = :i", i=inv))[0][0] == "pending_approval"
    return inv


async def decide(client, invoice_id, decision="approved", reason=None):
    return await client.post(f"/api/ap-approvals/{invoice_id}/decide", json={"decision": decision, "reason": reason})


async def status_of(team, invoice_id):
    return (await rows(team.org_id, "SELECT status FROM invoices WHERE id = :i", i=invoice_id))[0][0]


async def add_user(team, role, name):
    email = f"{name}-{team.org_id[:8]}@example.test"
    assert (
        await team.admin.post("/api/users", json={"email": email, "name": name, "role": role, "password": PASSWORD})
    ).status_code == 201
    return await team.login(email)


async def test_a_small_invoice_needs_one_approver(team):
    inv = await pending(team, price="45", qty="10")  # 450
    r = await decide(team.approver, inv)
    assert (
        r.status_code == 200
        and r.json()["status"] == "approved"
        and r.json()["approvals"] == 1
        and r.json()["required"] == 1
    )
    assert (await team.admin.patch(f"/api/invoices/{inv}/fields", json={"total": "1.00"})).status_code == 409


async def test_whoever_entered_or_corrected_an_invoice_cannot_approve_it(team):
    """Review Focus 5."""
    inv = await pending(team)  # entered by the clerk
    await team.admin.patch(f"/api/users/{team.user_ids['clerk']}", json={"role": "approver"})
    clerk_now_approver = await team.login(team.emails["clerk"])
    refused = await decide(clerk_now_approver, inv)
    assert refused.status_code == 403 and "entered" in refused.json()["detail"]

    other = await pending(team, number="P2")
    await rows(
        team.org_id,
        "UPDATE invoices SET corrected_by = ARRAY[CAST(:u AS uuid)] WHERE id = :i RETURNING 1",
        u=team.user_ids["approver"],
        i=other,
    )
    assert (await decide(team.approver, other)).status_code == 403
    assert (await decide(team.viewer, other)).status_code == 403  # viewers never decide


async def test_a_large_invoice_needs_two_people_one_of_them_an_admin(team):
    inv = await pending(team, price="1500", qty="10")  # 15,000
    first = await decide(team.approver, inv)
    assert (
        first.json()["status"] == "pending_approval"
        and first.json()["approvals"] == 1
        and first.json()["required"] == 2
    )
    second_approver = await add_user(team, "approver", "second")
    still = await decide(second_approver, inv)
    assert still.json()["status"] == "pending_approval" and "administrator" in still.json()["waiting_for"]
    done = await decide(team.admin, inv)
    assert done.json()["status"] == "approved"
    assert (await decide(team.approver, inv)).status_code == 409


async def test_two_approvers_at_the_same_instant_approve_once(team):
    """Review Focus 5: a one-approval invoice and two clicks at once."""
    inv = await pending(team)
    second_approver = await add_user(team, "approver", "racer")
    results = await asyncio.gather(decide(team.approver, inv), decide(second_approver, inv))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert (await rows(team.org_id, "SELECT count(*) FROM invoice_approvals WHERE invoice_id = :i", i=inv))[0][0] == 1
    assert await status_of(team, inv) == "approved"


async def test_a_rejection_ends_it_and_an_admin_can_reopen_an_approved_invoice(team):
    inv = await pending(team)
    assert (await decide(team.approver, inv, "rejected", "Not ours")).json()["status"] == "rejected"
    other = await pending(team, number="P3")
    await decide(team.approver, other)
    assert (
        await team.clerk.post(f"/api/invoices/{other}/reopen", json={"reason": "Wrong cost centre"})
    ).status_code == 403
    reopened = await team.admin.post(f"/api/invoices/{other}/reopen", json={"reason": "Wrong cost centre"})
    assert reopened.json()["status"] == "pending_approval"
    assert (await rows(team.org_id, "SELECT count(*) FROM invoice_approvals WHERE invoice_id = :i", i=other))[0][0] == 0
    assert (await team.admin.get("/api/audit", params={"action": "invoice.reopened"})).json()["entries"]


async def test_invoices_waiting_too_long_are_escalated_and_listed_first(team):
    from app.approvals.chain import escalate_overdue

    old = await pending(team, number="P4")
    new = await pending(team, number="P5")
    await rows(
        team.org_id, "UPDATE invoices SET pending_since = now() - interval '3 days' WHERE id = :i RETURNING 1", i=old
    )
    await escalate_overdue()
    queue = (await team.approver.get("/api/ap-approvals", params={"mine": "true"})).json()["invoices"]
    assert (
        [q["id"] for q in queue][:2] == [old, new] and queue[0]["escalated"] is True and queue[1]["escalated"] is False
    )


async def test_my_queue_leaves_out_what_i_may_not_approve(team):
    inv = await pending(team, number="P6")
    await rows(
        team.org_id, "UPDATE invoices SET entered_by = :u WHERE id = :i RETURNING 1", u=team.user_ids["approver"], i=inv
    )
    mine = (await team.approver.get("/api/ap-approvals", params={"mine": "true"})).json()["invoices"]
    assert inv not in [q["id"] for q in mine]
    assert inv in [
        q["id"] for q in (await team.admin.get("/api/ap-approvals", params={"mine": "true"})).json()["invoices"]
    ]


async def test_the_queue_costs_the_same_number_of_queries_however_many_invoices_wait(team):
    """Found at 100,000 invoices: four queries per waiting invoice made 'my approvals' take 1.2 s with 500 waiting."""
    from sqlalchemy import event

    from app.db import _engines

    async def statements() -> int:
        seen = []

        def count(*_):
            seen.append(1)

        engines = [e.sync_engine for e in _engines.values()]
        for e in engines:
            event.listen(e, "before_cursor_execute", count)
        try:
            assert (await team.approver.get("/api/ap-approvals", params={"mine": "true"})).status_code == 200
        finally:
            for e in engines:
                event.remove(e, "before_cursor_execute", count)
        return len(seen)

    await pending(team, number="Q1")
    await statements()  # the first request of a session does its own bookkeeping
    few = await statements()
    for n in range(2, 7):
        await pending(team, number=f"Q{n}")
    assert await statements() == few


async def test_a_waiting_invoice_can_be_sent_back_for_correction_and_its_approvals_are_cleared(team):
    """Found by the journeys: a waiting invoice could not be changed and 'reopen' applies only to approved ones."""
    inv = await pending(team, price="150", qty="10", number="SB1")  # 1,500: two approvals needed
    await decide(team.approver, inv)
    assert (
        await team.clerk.post(f"/api/invoices/{inv}/send-back", json={"reason": "Wrong due date"})
    ).status_code == 403
    sent = await team.admin.post(f"/api/invoices/{inv}/send-back", json={"reason": "Wrong due date"})
    assert sent.status_code == 200 and sent.json()["status"] == "ready"
    assert (await rows(team.org_id, "SELECT count(*) FROM invoice_approvals WHERE invoice_id = :i", i=inv))[0][0] == 0
    assert (await team.clerk.patch(f"/api/invoices/{inv}/fields", json={"due_date": "2026-12-31"})).status_code == 200
    assert (await team.admin.post(f"/api/invoices/{inv}/send-back", json={"reason": "again"})).status_code == 409
    assert (await team.admin.get("/api/audit", params={"action": "invoice.sent_back"})).json()["entries"]


async def test_an_approval_does_not_survive_a_rematch_or_correction(team):
    """C2: 4,500 needs two approvals. One is given, a clerk matches again, and the approval must not carry over."""
    inv = await pending(team, price="450", qty="10", number="P6")
    assert (await decide(team.approver, inv)).json()["approvals"] == 1
    again = await team.clerk.post(f"/api/invoices/{inv}/match", json={})
    assert again.status_code == 200
    assert (await rows(team.org_id, "SELECT count(*) FROM invoice_approvals WHERE invoice_id = :i", i=inv))[0][0] == 0
    cleared = await team.admin.get("/api/audit", params={"action": "invoice.approvals_cleared"})
    assert [e["target"] for e in cleared.json()["entries"]] == [inv]
    one_more = await decide(team.approver, inv)
    assert one_more.json()["approvals"] == 1 and one_more.json()["status"] == "pending_approval"
    assert await status_of(team, inv) == "pending_approval"


async def test_whoever_created_or_changed_the_order_or_its_receipt_cannot_approve_its_invoice(team):
    """I8: an approver who made, changed or received against the invoice's purchase order may not approve it."""
    vendor = (
        await team.admin.post("/api/vendors", json={"name": "Order Party Ltd", "default_currency": "USD"})
    ).json()["id"]

    async def order(client, number):
        r = await client.post("/api/orders", json={"vendor_id": vendor, "po_number": number, "currency": "USD", "lines": [
            {"item_code": "W-1", "description": "Widget, steel", "quantity": "10", "unit_price": "4.50"}]})  # fmt: skip
        assert r.status_code == 201, r.text
        return r.json()

    async def receive(client, po):
        r = await client.post(f"/api/orders/{po['id']}/receipts", json={"receipt_no": f"GRN-{po['po_number']}",
                              "lines": [{"po_line_id": po["lines"][0]["id"], "quantity": "10"}]})  # fmt: skip
        assert r.status_code == 201, r.text

    created = await order(team.approver, "PO-I8A")  # the approver created the order
    await receive(team.clerk, created)
    changed = await order(team.clerk, "PO-I8B")  # the approver changed it
    await team.approver.patch(
        f"/api/orders/{changed['id']}", json={"lines": [{"id": changed["lines"][0]["id"], "unit_price": "4.50"}]}
    )
    await receive(team.clerk, changed)
    received = await order(team.clerk, "PO-I8C")  # the approver recorded the receipt
    await receive(team.approver, received)

    invoices = {}
    for number, po_number, qty in (("I8A", "PO-I8A", "10"), ("I8B", "PO-I8B", "9"), ("I8C", "PO-I8C", "8")):
        # different amounts, so that the duplicate-amount check does not hold them
        invoices[number] = await make_invoice(team, vendor, number=number, qty=qty, price="4.50", po=po_number)
        assert (await match(team, invoices[number]))["status"] == "matched"
    for number, inv in invoices.items():
        refused = await decide(team.approver, inv)
        assert refused.status_code == 403 and "purchase order" in refused.json()["detail"], (number, refused.text)
    assert (await decide(team.admin, invoices["I8A"])).status_code == 200  # someone uninvolved may approve


async def test_the_queue_says_how_each_invoice_was_matched_and_who_accepted_a_variance(team):
    """I12: the queue shows the match (and its value), and an exception resolved by a person, with who did it."""
    from tests.test_matching import make_invoice as invoice_for
    from tests.test_matching import setup_po as order_for

    clean = await pending(team, price="45", qty="10", number="Q1")
    vendor, _ = await order_for(team, ordered="10", received="10", price="4.50", po_number="PO-Q2")
    varied = await invoice_for(
        team, vendor, number="Q2", qty="10", price="4.5675", po="PO-Q2"
    )  # 1.5%, within tolerance
    assert (await team.clerk.post(f"/api/invoices/{varied}/match", json={})).json()["status"] == "matched_with_variance"
    vendor3, _ = await order_for(team, ordered="10", received="10", price="4.50", po_number="PO-Q3")
    over = await invoice_for(team, vendor3, number="Q3", qty="10", price="4.635", po="PO-Q3")  # 3%: an exception
    assert (await team.clerk.post(f"/api/invoices/{over}/match", json={})).json()["status"] == "exception"
    exc = (await rows(team.org_id, "SELECT id FROM exceptions WHERE invoice_id = :i", i=over))[0][0]
    assert (
        await team.approver.post(f"/api/exceptions/{exc}/resolve", json={"outcome": "approve_with_variance"})
    ).status_code == 200

    queue = {i["invoice_number"]: i for i in (await team.approver.get("/api/ap-approvals")).json()["invoices"]}
    assert queue["Q1"]["match"] == {"status": "matched", "value": "450.00"} and queue["Q1"]["exception"] is None
    assert (
        queue["Q2"]["match"] == {"status": "matched_with_variance", "value": "45.68"}
        and queue["Q2"]["exception"] is None
    )
    assert queue["Q3"]["exception"] == {"resolution": "approve_with_variance", "resolved_by": "Approver"}
    assert clean and queue["Q3"]["match"]["status"] == "exception"
