"""The agent loop, driven by a scripted model and a toy tool set."""

import json

from app.agent.context import build_messages
from app.agent.runtime import advance
from app.llm.stub import reply
from app.llm.types import ToolCall
from app.redis import get_redis
from tests.agent_helpers import TOY, audit_targets, call, decide, done, effects, get_run, get_steps, new_run


async def test_happy_path_runs_tools_in_order_and_completes(team, stub_llm):
    stub_llm.script = [call("echo", text="one"), call("echo", "c2", text="two"), done("the answer")]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "completed"
    steps = await get_steps(team, run_id)
    assert [(s.kind, s.name if s.kind == "tool" else "") for s in steps] == [
        ("model", ""), ("tool", "echo"), ("model", ""), ("tool", "echo"), ("model", ""), ("tool", "finish"),
    ]  # fmt: skip
    assert [s.idx for s in steps] == list(range(6))
    run = await get_run(team, run_id)
    assert run.result == {"summary": "the answer"} and run.steps_used == 3 and run.tokens_used == 360
    assert run.finished_at is not None and run.error is None


async def test_the_model_sees_every_earlier_tool_result(team, stub_llm):
    stub_llm.script = [call("echo", text="hello"), done()]
    run_id = await new_run(team, question="say hello")
    await advance(run_id, TOY)
    second_call = stub_llm.calls[1]
    assert [m.role for m in second_call] == ["system", "user", "assistant", "tool"]
    assert second_call[1].content == "say hello" and second_call[2].tool_calls[0].name == "echo"
    assert json.loads(second_call[3].content) == {"summary": "echo hello", "data": {"text": "hello"}}


async def test_parallel_tool_calls_in_one_turn_are_all_answered(team, stub_llm):
    both = reply(tool_calls=[ToolCall("a", "echo", {"text": "1"}), ToolCall("b", "echo", {"text": "2"})])
    stub_llm.script = [both, done()]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "completed"
    assert [m.tool_call_id for m in stub_llm.calls[1] if m.role == "tool"] == ["a", "b"]


async def test_unknown_tool_and_bad_arguments_are_told_to_the_model(team, stub_llm):
    stub_llm.script = [call("teleport"), call("echo", "c2", text="ok"), call("echo", "c3", wrong=1), done()]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "completed"
    errors = [s.error for s in await get_steps(team, run_id) if s.kind == "tool" and s.error]
    assert "no tool named 'teleport'" in errors[0] and "echo" in errors[0]
    assert "text: Field required" in errors[1]


async def test_two_invalid_calls_in_a_row_fail_the_run(team, stub_llm):
    stub_llm.script = [call("teleport"), call("echo", "c2", wrong=1), done()]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "failed"
    assert "incorrectly twice in a row" in (await get_run(team, run_id)).error


async def test_unparseable_arguments_are_reported_as_invalid(team, stub_llm):
    bad = reply(tool_calls=[ToolCall("x", "echo", {}, invalid=True, raw='{"text": "unterminated')])
    stub_llm.script = [bad, done()]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "completed"
    assert "not a JSON object" in (await get_steps(team, run_id))[1].error


async def test_a_tool_the_role_may_not_use_does_not_exist_for_it(team, stub_llm):
    stub_llm.script = [call("admin_only"), done()]
    run_id = await new_run(team, role="clerk")
    await advance(run_id, TOY)
    assert "no tool named 'admin_only'" in (await get_steps(team, run_id))[1].error
    assert "admin_only" not in {t.name for t in TOY.registry.specs("clerk")}
    assert "admin_only" in {t.name for t in TOY.registry.specs("admin")}
    assert TOY.registry.specs("viewer") == []


async def test_tool_failures_are_recorded_and_shown_without_internal_detail(team, stub_llm):
    stub_llm.script = [call("refuse"), call("boom"), call("slow"), done()]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "completed"
    errors = [s.error for s in await get_steps(team, run_id) if s.kind == "tool" and s.error]
    assert errors == [
        "there is no such thing",
        "The tool failed unexpectedly.",
        "The tool timed out after 0.2 seconds.",
    ]
    assert "secret internal detail" not in " ".join(m.content for m in stub_llm.calls[-1])
    assert stub_llm.calls[1][-1].content == "ERROR: there is no such thing"


async def test_large_results_reach_the_model_as_summary_and_handle(team, stub_llm):
    stub_llm.script = [call("big"), done()]
    run_id = await new_run(team)
    await advance(run_id, TOY)
    shown = json.loads(stub_llm.calls[1][-1].content)
    assert shown["summary"] == "500 rows" and shown["handle"] == "ev_big00001" and "data" not in shown
    assert len((await get_steps(team, run_id))[1].output["data"]["rows"]) == 500  # stored in full


async def test_secrets_are_masked_in_stored_steps_and_events(team, stub_llm):
    stub_llm.script = [call("leak"), done()]
    run_id = await new_run(team)
    await advance(run_id, TOY)
    out = (await get_steps(team, run_id))[1].output
    assert out["summary"] == "found [redacted] in a file" and out["data"] == {"api_key": "[redacted]", "value": 1}
    events = json.dumps(await get_redis().xrange(f"run:{run_id}"))
    assert "hunter2" not in events and "sk-abcdefghijklmnop" not in events


async def test_prose_instead_of_a_tool_call_gets_one_reminder_then_fails(team, stub_llm):
    stub_llm.script = [reply("I think the answer is 42."), done("42")]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "completed"
    assert "You did not call a tool" in stub_llm.calls[1][-1].content

    stub_llm.script = [reply("thinking"), reply("still thinking")]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "failed"
    assert "stopped using tools" in (await get_run(team, run_id)).error


async def test_database_effects_commit_together_with_the_step(team, stub_llm):
    stub_llm.script = [call("note", text="first"), done()]
    run_id = await new_run(team)
    await advance(run_id, TOY)
    assert await audit_targets(team, "toy.note") == ["first"]


async def test_approval_parks_the_run_without_executing(team, stub_llm):
    effects.clear()
    stub_llm.script = [call("launch", text="rocket")]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "waiting_approval"
    run = await get_run(team, run_id)
    assert run.status == "waiting_approval" and run.pending["tool"] == "launch" and effects == []
    assert (await get_steps(team, run_id))[1].state == "awaiting_approval"
    assert await advance(run_id, TOY) == "waiting_approval" and len(stub_llm.calls) == 1  # nothing happens


async def test_approved_action_runs_exactly_once_then_the_run_continues(team, stub_llm):
    effects.clear()
    stub_llm.script = [call("launch", text="rocket"), done("launched")]
    run_id = await new_run(team)
    await advance(run_id, TOY)
    await decide(team, run_id, "approved")
    assert await advance(run_id, TOY) == "completed"
    assert await advance(run_id, TOY) == "completed"
    assert effects == ["rocket"]
    assert json.loads(stub_llm.calls[1][-1].content)["summary"] == "launched rocket"


async def test_rejected_action_never_runs_and_the_model_is_told_why(team, stub_llm):
    effects.clear()
    stub_llm.script = [call("launch", text="rocket"), done("not launched")]
    run_id = await new_run(team)
    await advance(run_id, TOY)
    await decide(team, run_id, "rejected", "too risky")
    assert await advance(run_id, TOY) == "completed"
    assert effects == []
    assert stub_llm.calls[1][-1].content == "ERROR: An administrator rejected this action: too risky"


async def test_question_to_the_user_parks_the_run(team, stub_llm):
    stub_llm.script = [call("ask_user", text="Which year?")]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "waiting_user"
    run = await get_run(team, run_id)
    assert run.pending == {"kind": "question", "question": "Which year?", "step_idx": 1}
    assert await advance(run_id, TOY) == "waiting_user"


async def test_advancing_a_finished_run_does_nothing(team, stub_llm):
    stub_llm.script = [done()]
    run_id = await new_run(team)
    await advance(run_id, TOY)
    before = len(await get_steps(team, run_id))
    assert await advance(run_id, TOY) == "completed" and len(await get_steps(team, run_id)) == before
    assert len(stub_llm.calls) == 1


async def test_conversation_is_rebuilt_identically_from_the_database(team, stub_llm):
    stub_llm.script = [call("echo", text="a"), call("refuse"), done()]
    run_id = await new_run(team, question="q")
    await advance(run_id, TOY)
    rebuilt = build_messages("q", (await get_steps(team, run_id))[:4], TOY.system_prompt)
    assert rebuilt == stub_llm.calls[2]


async def test_cancel_request_stops_the_run_before_the_next_step(team, stub_llm):
    from sqlalchemy import update

    from app.db import tenant_session
    from app.models.runs import Run

    stub_llm.script = [call("echo", text="a"), done()]
    run_id = await new_run(team)
    async with tenant_session(run.org_id if (run := await get_run(team, run_id)) else None) as db:
        await db.execute(update(Run).where(Run.id == run_id).values(cancel_requested=True))
    assert await advance(run_id, TOY) == "cancelled" and stub_llm.calls == []


async def test_model_failure_fails_the_run_with_a_readable_error(team, stub_llm):
    from app.llm.base import LLMError

    stub_llm.script = [LLMError("Provider returned 401: bad key", status=401)]
    run_id = await new_run(team)
    assert await advance(run_id, TOY) == "failed"
    assert "401" in (await get_run(team, run_id)).error


async def test_events_are_published_in_order(team, stub_llm):
    stub_llm.script = [call("echo", text="a"), done()]
    run_id = await new_run(team)
    await advance(run_id, TOY)
    types = [fields["type"] for _, fields in await get_redis().xrange(f"run:{run_id}")]
    assert types == [
        "step.started", "step.finished", "step.started", "step.finished", "step.started", "step.finished",
        "step.started", "step.finished", "report.ready", "run.status",
    ]  # fmt: skip
