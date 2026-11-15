"""Usage figures add up to the recorded steps, and people see only what they may."""

from sqlalchemy import text

from app.db import tenant_session
from tests.agent_helpers import call, done, work
from tests.test_investigations_api import ask


async def some_usage(team, stub_llm) -> None:
    stub_llm.script = [call("echo", text="a"), done()]
    await ask(team.clerk)
    await work()
    stub_llm.script = [done()]
    await ask(team.admin)
    await work()


async def test_totals_equal_the_sum_of_the_step_rows(empty_queue, team, stub_llm):
    await some_usage(team, stub_llm)
    async with tenant_session(team.org_id) as db:
        calls, tokens_in, tokens_out, cost = (
            await db.execute(
                text("SELECT count(*), sum(tokens_in), sum(tokens_out), sum(cost_usd) FROM steps WHERE kind = 'model'")
            )
        ).one()
    for group in ("day", "user", "model"):
        body = (await team.admin.get("/api/usage", params={"group": group})).json()
        assert body["scope"] == "organisation"
        assert body["totals"] == {
            "model_calls": calls == 3 and 3, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost_usd": float(cost),
        }  # fmt: skip
    by_user = {i["key"]: i for i in (await team.admin.get("/api/usage", params={"group": "user"})).json()["items"]}
    assert by_user[team.emails["clerk"]]["model_calls"] == 2 and by_user[team.emails["admin"]]["runs"] == 1
    assert (await team.admin.get("/api/usage", params={"group": "model"})).json()["items"][0]["key"] == "stub"


async def test_clerks_see_only_their_own_usage_and_viewers_none(empty_queue, team, stub_llm):
    await some_usage(team, stub_llm)
    mine = (await team.clerk.get("/api/usage", params={"group": "user"})).json()
    assert mine["scope"] == "you" and [i["key"] for i in mine["items"]] == [team.emails["clerk"]]
    assert mine["totals"]["model_calls"] == 2 and mine["budget"]["org_spent_today_usd"] is None
    assert (await team.viewer.get("/api/usage")).status_code == 403


async def test_usage_is_private_to_the_organisation_and_shows_the_budget(empty_queue, team, other_team, stub_llm):
    await some_usage(team, stub_llm)
    other = (await other_team.admin.get("/api/usage")).json()
    assert other["items"] == [] and other["totals"]["model_calls"] == 0 and other["budget"]["org_spent_today_usd"] == 0
    ours = (await team.admin.get("/api/usage")).json()["budget"]
    assert ours["org_spent_today_usd"] > 0 and ours["org_daily_cost_usd"] == 20.0 and ours["cost_is_estimate"] is True


async def test_invalid_ranges_are_rejected(team):
    bad = {"from": "2026-05-01T00:00:00Z", "to": "2026-01-01T00:00:00Z"}
    assert (await team.admin.get("/api/usage", params=bad)).status_code == 422
    assert (await team.admin.get("/api/usage", params={"from": "2020-01-01T00:00:00Z"})).status_code == 422
    assert (await team.admin.get("/api/usage", params={"group": "secret"})).status_code == 422
