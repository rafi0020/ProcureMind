"""Approvals over HTTP: only administrators decide, each request is decided once, the run then continues."""

from tests.agent_helpers import call, done, effects, work
from tests.test_investigations_api import ask


async def parked_run(team, stub_llm) -> tuple[str, str]:
    effects.clear()
    stub_llm.script = [call("launch", text="rocket")]
    run_id = await ask(team.clerk)
    await work()
    pending = (await team.admin.get("/api/approvals")).json()["approvals"]
    assert [a["run_id"] for a in pending] == [run_id]
    return run_id, pending[0]["id"]


async def test_pending_approval_is_listed_with_what_was_asked(empty_queue, team, stub_llm):
    run_id, _ = await parked_run(team, stub_llm)
    item = (await team.admin.get("/api/approvals")).json()["approvals"][0]
    assert item["tool"] == "launch" and item["arguments"] == {"text": "rocket"} and item["status"] == "pending"
    assert item["requested_by"] == team.emails["clerk"] and item["question"]
    assert (await team.clerk.get(f"/api/investigations/{run_id}")).json()["status"] == "waiting_approval"
    assert (await team.viewer.get("/api/approvals")).status_code == 403
    assert len((await team.clerk.get("/api/approvals")).json()["approvals"]) == 1  # the clerk's own request


async def test_only_an_administrator_can_decide(empty_queue, team, stub_llm):
    _, approval = await parked_run(team, stub_llm)
    for client in (team.clerk, team.viewer):
        r = await client.post(f"/api/approvals/{approval}/decide", json={"decision": "approved"})
        assert r.status_code == 403
    assert effects == []


async def test_approval_runs_the_action_once_and_is_audited(empty_queue, team, stub_llm):
    run_id, approval = await parked_run(team, stub_llm)
    stub_llm.script = [done("launched")]
    r = await team.admin.post(f"/api/approvals/{approval}/decide", json={"decision": "approved", "reason": "ok"})
    assert r.json() == {"status": "approved"}
    again = await team.admin.post(f"/api/approvals/{approval}/decide", json={"decision": "approved"})
    assert again.status_code == 409 and "already approved" in again.json()["detail"]
    await work()
    await work()
    assert effects == ["rocket"]
    assert (await team.clerk.get(f"/api/investigations/{run_id}")).json()["status"] == "completed"
    entry = (await team.admin.get("/api/audit", params={"action": "approval."})).json()["entries"][0]
    assert entry["action"] == "approval.approved" and entry["user"] == team.emails["admin"]
    assert entry["detail"]["tool"] == "launch" and entry["detail"]["run_id"] == run_id
    assert (await team.admin.get("/api/approvals")).json()["approvals"] == []
    assert len((await team.admin.get("/api/approvals", params={"status": "all"})).json()["approvals"]) == 1


async def test_rejection_drops_the_action_and_tells_the_model(empty_queue, team, stub_llm):
    run_id, approval = await parked_run(team, stub_llm)
    stub_llm.script = [done("not launched")]
    await team.admin.post(f"/api/approvals/{approval}/decide", json={"decision": "rejected", "reason": "too risky"})
    await work()
    assert effects == []
    steps = (await team.clerk.get(f"/api/investigations/{run_id}")).json()["steps"]
    assert steps[1]["error"] == "An administrator rejected this action: too risky"


async def test_another_organisation_cannot_see_or_decide(empty_queue, team, other_team, stub_llm):
    _, approval = await parked_run(team, stub_llm)
    assert (await other_team.admin.get("/api/approvals")).json()["approvals"] == []
    r = await other_team.admin.post(f"/api/approvals/{approval}/decide", json={"decision": "approved"})
    assert r.status_code == 404 and effects == []


async def test_invalid_decision_is_rejected(empty_queue, team, stub_llm):
    _, approval = await parked_run(team, stub_llm)
    r = await team.admin.post(f"/api/approvals/{approval}/decide", json={"decision": "maybe"})
    assert r.status_code == 422


async def test_two_people_for_an_action_the_agent_proposed_unless_only_one_person_may_decide(
    empty_queue, team, stub_llm
):
    """Whoever started a run does not approve what it proposes, unless they are the only approver or administrator
    in the organisation (recorded as sole_admin)."""
    effects.clear()
    await team.admin.patch(f"/api/users/{team.user_ids['approver']}", json={"is_active": False})
    stub_llm.script = [call("launch", text="alone")]
    await ask(team.admin)
    await work()
    approval = (await team.admin.get("/api/approvals")).json()["approvals"][0]
    assert (
        await team.admin.post(f"/api/approvals/{approval['id']}/decide", json={"decision": "rejected"})
    ).status_code == 200
    stub_llm.script = [done()]  # the rejected run continues and finishes
    await work()

    await team.admin.patch(f"/api/users/{team.user_ids['approver']}", json={"is_active": True})
    stub_llm.script = [call("launch", text="two")]
    run_id = await ask(team.admin)
    await work()
    approval = next(a for a in (await team.admin.get("/api/approvals")).json()["approvals"] if a["run_id"] == run_id)
    own = await team.admin.post(f"/api/approvals/{approval['id']}/decide", json={"decision": "approved"})
    assert own.status_code == 403
    approver = await team.login(team.emails["approver"])
    decided = await approver.post(f"/api/approvals/{approval['id']}/decide", json={"decision": "approved"})
    assert decided.json()["status"] == "approved"
