"""Regression tests for defects found by the whole-branch review."""

import pytest
from pydantic import BaseModel
from sqlalchemy import update

from app.agent.runtime import advance
from app.agent.tools import AgentSpec, ToolContext, ToolRegistry, ToolResult
from app.config import get_settings
from app.db import tenant_session
from app.llm import factory
from app.models.runs import Run
from app.worker.run import claim_next, release
from tests.agent_helpers import TOY, call, done, get_run, new_run, work
from tests.test_investigations_api import ask


async def test_answering_and_approving_do_not_use_up_the_crash_allowance(empty_queue, team, stub_llm, monkeypatch):
    monkeypatch.setattr(get_settings(), "run_max_attempts", 2)
    run_id = None
    for round_ in range(4):  # four pauses: more claims than the crash allowance
        stub_llm.script = [call("ask_user", f"q{round_}", text=f"Question {round_}?")]
        if run_id is None:
            run_id = await ask(team.clerk)
        await work()
        assert (await team.clerk.get(f"/api/investigations/{run_id}")).json()["status"] == "waiting_user"
        assert (await team.clerk.post(f"/api/investigations/{run_id}/answer", json={"text": "yes"})).status_code == 200
    stub_llm.script = [done("finished after four answers")]
    await work()
    body = (await team.clerk.get(f"/api/investigations/{run_id}")).json()
    assert body["status"] == "completed", body["error"]


async def test_a_graceful_handover_is_not_counted_as_a_crash(empty_queue, team):
    run_id = await new_run(team, status="queued")
    await claim_next("w1")
    await release(run_id, "w1", retry_now=True)  # what a worker does on SIGTERM
    await claim_next("w2")
    assert (await get_run(team, run_id)).attempts == 1


async def test_a_configuration_error_fails_the_run_with_its_reason(team, monkeypatch):
    async def broken(org_id):
        raise ValueError("Base URL host cannot be resolved.")

    monkeypatch.setattr(factory, "get_client", broken)
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "failed"
    assert "Base URL host cannot be resolved" in (await get_run(team, run_id)).error


async def test_tools_receive_their_arguments_unaltered_and_the_api_masks_them(empty_queue, team, stub_llm):
    received = {}
    registry = ToolRegistry()

    class Args(BaseModel):
        sql: str
        access_token: str  # named like a secret: masked when shown, intact for the tool

    @registry.tool("probe", "Records what it was given.")
    async def probe(ctx: ToolContext, args: Args) -> ToolResult:
        received.update(args.model_dump())
        return ToolResult("ok")

    @registry.tool("finish", "Finish.")
    async def finish(ctx: ToolContext, args: Args) -> ToolResult:
        return ToolResult("done", data={"summary": "x"}, signal="finish")

    literal = "SELECT 'sk-abcdefghijklmnopqrstuvwx' AS code"
    stub_llm.script = [call("probe", sql=literal, access_token="t-7"), call("finish", sql="x", access_token="t-1")]
    run_id = await new_run(team)
    assert await advance(run_id, AgentSpec("test", registry)) == "completed"
    assert received == {"sql": literal, "access_token": "t-7"}  # found in review: the tool used to get "[redacted]"
    shown = (await team.clerk.get(f"/api/investigations/{run_id}")).json()["steps"][1]["input"]
    assert "sk-abcdefghij" not in shown["sql"] and shown["access_token"] == "[redacted]"


async def test_a_redis_failure_while_publishing_does_not_fail_the_run(team, stub_llm, monkeypatch):
    from app.agent import runtime

    async def down(*args, **kwargs):
        raise ConnectionError("redis is down")

    monkeypatch.setattr(runtime, "_publish_to_redis", down)
    stub_llm.script = [call("echo", text="a"), done()]
    assert await advance(await new_run(team), TOY) == "completed"


async def test_spending_across_all_organisations_is_capped(team, other_team, stub_llm, monkeypatch):
    monkeypatch.setattr(get_settings(), "global_daily_cost_usd", 0.0000001)
    async with tenant_session(other_team.org_id) as db:  # someone else has already spent today
        db.add(Run(org_id=other_team.org_id, role="clerk", question="earlier", status="completed", cost_usd=1))
    stub_llm.script = [done()]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "budget_exceeded"
    assert "installation's daily limit" in (await get_run(team, run_id)).error


def test_the_public_demo_secret_is_refused_when_serving_over_https(monkeypatch):
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "cookie_secure", True)
    monkeypatch.setattr(get_settings(), "secret_key", "local-demo-secret-change-me")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()


async def test_cancelling_a_run_closes_its_pending_approval(empty_queue, team, stub_llm):
    stub_llm.script = [call("launch", text="rocket")]
    run_id = await ask(team.clerk)
    await work()
    assert (await team.clerk.post(f"/api/investigations/{run_id}/cancel")).json() == {"status": "cancelled"}
    assert (await team.admin.get("/api/approvals")).json()["approvals"] == []
    async with tenant_session(team.org_id) as db:
        await db.execute(update(Run).where(Run.id == run_id).values(status="cancelled"))
