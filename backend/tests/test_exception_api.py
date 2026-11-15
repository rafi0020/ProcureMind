"""Exceptions and the agent over HTTP: start an investigation, read its recommendation and drafts, accept it."""

from app.exceptions.agent import AGENT
from tests.agent_helpers import work
from tests.conftest import rows
from tests.test_matching import make_invoice, setup_po


async def open_exception(team, number="X1") -> tuple[str, str]:
    vendor, _ = await setup_po(team, po_number=f"PO-{number}")
    inv = await make_invoice(team, vendor, number=number, qty="1", price="4.50", po="PO-404")  # no such PO
    await team.clerk.post(f"/api/invoices/{inv}/match", json={})
    exc = next(e for e in (await team.clerk.get("/api/exceptions")).json()["exceptions"] if e["invoice_id"] == inv)
    return inv, exc["id"]


async def investigate(team, client, exception_id) -> str:
    r = await client.post(f"/api/exceptions/{exception_id}/investigate")
    assert r.status_code == 202, r.text
    await work(AGENT)  # the built-in demonstration script: it recommends a hold
    return r.json()["run_id"]


async def test_an_investigation_ends_in_a_recommendation_that_an_approver_accepts_once(empty_queue, team):
    inv, exc = await open_exception(team)
    assert (await team.viewer.post(f"/api/exceptions/{exc}/investigate")).status_code == 403
    run_id = await investigate(team, team.clerk, exc)
    detail = (await team.clerk.get(f"/api/exceptions/{exc}")).json()
    assert (
        detail["status"] == "recommended"
        and detail["recommendation"]["outcome"] == "hold"
        and detail["recommendation"]["run_id"] == run_id
    )
    assert (await team.clerk.get(f"/api/investigations/{run_id}")).json()["status"] == "completed"

    assert (await team.clerk.post(f"/api/exceptions/{exc}/accept-recommendation")).status_code == 403
    accepted = await team.approver.post(f"/api/exceptions/{exc}/accept-recommendation")
    assert accepted.status_code == 200 and accepted.json()["resolution"] == "hold"
    assert (await rows(team.org_id, "SELECT status FROM invoices WHERE id = :i", i=inv))[0][0] == "on_hold"
    assert (await team.approver.post(f"/api/exceptions/{exc}/accept-recommendation")).status_code == 409
    entry = (await team.admin.get("/api/audit", params={"action": "exception.resolved"})).json()["entries"][0]
    assert entry["detail"]["source"] == "agent"


async def test_whoever_started_the_investigation_does_not_accept_its_recommendation(empty_queue, team):
    _, exc = await open_exception(team, "X2")
    await investigate(team, team.approver, exc)
    refused = await team.approver.post(f"/api/exceptions/{exc}/accept-recommendation")
    assert refused.status_code == 403 and "started" in refused.json()["detail"]
    assert (await team.admin.post(f"/api/exceptions/{exc}/accept-recommendation")).status_code == 200


async def test_drafts_are_listed_and_nothing_is_recommended_until_the_agent_ran(empty_queue, team):
    _, exc = await open_exception(team, "X3")
    assert (await team.approver.post(f"/api/exceptions/{exc}/accept-recommendation")).status_code == 409
    await rows(
        team.org_id,
        "INSERT INTO drafts (org_id, exception_id, kind, body) VALUES (:o, :e, 'credit_note_request', 'Please credit 1.00') RETURNING 1",
        o=team.org_id,
        e=exc,
    )
    drafts = (await team.viewer.get(f"/api/exceptions/{exc}/drafts")).json()["drafts"]
    assert drafts[0]["kind"] == "credit_note_request" and drafts[0]["body"] == "Please credit 1.00"


async def test_new_exceptions_are_investigated_automatically_when_switched_on(empty_queue, team, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "auto_investigate", True)
    _, exc = await open_exception(team, "X4")
    queued = await rows(team.org_id, "SELECT question, status FROM runs WHERE question LIKE :q", q=f"%{exc}%")
    assert len(queued) == 1 and queued[0].status == "queued"
    assert (await team.clerk.get(f"/api/exceptions/{exc}")).json()["status"] == "investigating"


async def test_other_organisations_cannot_investigate_or_accept(empty_queue, team, other_team):
    _, exc = await open_exception(team, "X5")
    assert (await other_team.clerk.post(f"/api/exceptions/{exc}/investigate")).status_code == 404
    assert (await other_team.admin.post(f"/api/exceptions/{exc}/accept-recommendation")).status_code == 404


async def exception_on_a_bad_order(team, number) -> tuple[str, str]:
    """An invoice for a purchase order that does not exist: matching stops it with an exception."""
    from tests.test_matching import make_invoice, match, setup_po

    vendor, _ = await setup_po(team)
    inv = await make_invoice(team, vendor, number=number, qty="10", price="4.50", po="nope")
    assert (await match(team, inv))["status"] == "exception"
    exc = (await rows(team.org_id, "SELECT id FROM exceptions WHERE invoice_id = :i", i=inv))[0][0]
    return inv, str(exc)


async def test_resolving_an_exception_requires_the_invoice_to_still_be_in_exception(empty_queue, team):
    """I1: a correction made the invoice ready; the exception raised on the old values must not move it on."""
    inv, exc = await exception_on_a_bad_order(team, "I1A")
    fixed = await team.clerk.patch(f"/api/invoices/{inv}/fields", json={"po_number": "PO-00123"})
    assert fixed.json()["status"] == "ready"
    refused = await team.approver.post(f"/api/exceptions/{exc}/resolve", json={"outcome": "approve_with_variance"})
    assert refused.status_code == 409
    assert (await rows(team.org_id, "SELECT status FROM invoices WHERE id = :i", i=inv))[0][0] == "ready"


async def test_a_correction_clears_the_recommendation_and_a_clean_rematch_closes_the_exception(empty_queue, team):
    """I1: a correction reopens the exception without its recommendation; a clean match closes it, audited."""
    from tests.test_matching import match

    inv, exc = await exception_on_a_bad_order(team, "I1B")
    await rows(
        team.org_id,
        "UPDATE exceptions SET status = 'recommended', recommendation = CAST(:r AS jsonb) WHERE id = :e RETURNING 1",
        r='{"outcome": "hold", "rationale": "old", "evidence": [], "run_id": null}',
        e=exc,
    )
    await team.clerk.patch(f"/api/invoices/{inv}/fields", json={"po_number": "PO-00123"})
    row = (await rows(team.org_id, "SELECT status, recommendation FROM exceptions WHERE id = :e", e=exc))[0]
    assert row.status == "open" and row.recommendation is None

    assert (await match(team, inv))["status"] == "matched"
    closed = (await rows(team.org_id, "SELECT status, resolution FROM exceptions WHERE id = :e", e=exc))[0]
    assert (closed.status, closed.resolution) == ("resolved", "cleared_by_rematch")
    entries = (await team.admin.get("/api/audit", params={"action": "exception.cleared_by_rematch"})).json()["entries"]
    assert [e["target"] for e in entries] == [exc]
