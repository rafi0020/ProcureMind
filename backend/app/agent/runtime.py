"""The agent loop.

    load the run and its steps from the database
    decide the next action from the steps alone
        - a tool call is waiting (approval, an answer, or a crash left it unfinished)  -> resolve it
        - the last model message asked for a tool that has not been started             -> start it
        - otherwise                                                                     -> ask the model
    carry out exactly one action, commit it, publish an event, repeat

There is no in-memory state between iterations. Anything that stops the loop (a crash, an approval, a
question to the user, a budget) leaves the run in a state from which calling `advance` again continues.
"""

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.agent.budgets import BudgetExceeded, check_budget
from app.agent.context import build_messages
from app.agent.events import publish as _publish_to_redis
from app.agent.tools import AgentSpec, Tool, ToolContext, ToolError, ToolResult
from app.db import system_session, tenant_session
from app.llm import factory
from app.llm.base import LLMError
from app.models.runs import TERMINAL, Approval, Run, Step
from app.obs import tracing
from app.obs.logging import run_id_var
from app.security.redact import redact

log = logging.getLogger("procuremind.agent")

INVALID = "invalid_call: "
NUDGE = "You did not call a tool. Continue by calling a tool; when you have the answer, call `finish`."
MAX_ITERATIONS = 400


async def publish(run_id: UUID, event_type: str, data: dict | None = None) -> None:
    """Events are a convenience for open browser tabs. The step is already committed, so if Redis is
    unreachable the run carries on and the page catches up from the database."""
    try:
        await _publish_to_redis(run_id, event_type, data)
    except Exception:  # noqa: BLE001
        log.warning("could not publish event", extra={"event": event_type})


class RunConflict(Exception):
    """Another worker is advancing this run (its lease expired here). This worker must let go."""


@dataclass
class Action:
    kind: str  # "model" | "start_tool" | "resolve"
    step: Step | None = None
    call: dict | None = None


def next_action(steps: list[Step]) -> Action:
    last_model = max((i for i, s in enumerate(steps) if s.kind == "model"), default=None)
    if last_model is None:
        return Action("model")
    after = steps[last_model + 1 :]
    for step in after:
        if step.kind == "tool" and step.state != "done":
            return Action("resolve", step=step)
    answered = {s.call_id for s in after if s.kind == "tool"}
    for call in (steps[last_model].output or {}).get("tool_calls", []):
        if call["id"] not in answered:
            return Action("start_tool", call=call)
    return Action("model")


def _key(run_id: UUID, idx: int, name: str, payload) -> str:
    """Identifies one step attempt. The unique index on it makes recording a step twice impossible."""
    raw = f"{run_id}|{idx}|{name}|{json.dumps(payload, sort_keys=True, default=str)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


async def _load(org_id: UUID, run_id: UUID) -> tuple[Run, list[Step]]:
    async with tenant_session(org_id) as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        steps = (await db.execute(select(Step).where(Step.run_id == run_id).order_by(Step.idx))).scalars().all()
    return run, list(steps)


async def _end(org_id: UUID, run_id: UUID, status: str, *, error: str | None = None) -> str:
    async with tenant_session(org_id) as db:
        await db.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status=status, error=error, finished_at=_now(), lease_owner=None, lease_expires_at=None, pending=None
            )
        )
    await publish(run_id, "run.status", {"status": status, "error": error})
    return status


async def _insert_step(org_id: UUID, step: Step, *, run_values: dict | None = None, extra=None) -> None:
    """Record a step, with any run update that belongs to it, in one transaction."""
    try:
        async with tenant_session(org_id) as db:
            db.add(step)
            if extra is not None:
                db.add(extra)
            if run_values:
                await db.execute(update(Run).where(Run.id == step.run_id).values(**run_values))
    except IntegrityError as exc:
        raise RunConflict(f"step {step.idx} of run {step.run_id} was recorded by another worker") from exc


def _step_event(step: Step) -> dict:
    out = step.output or {}
    return {
        "idx": step.idx, "kind": step.kind, "name": step.name, "state": step.state, "error": step.error,
        "summary": out.get("summary") or out.get("text") or "", "handle": out.get("handle"),
        "input": step.input if step.kind == "tool" else None, "latency_ms": step.latency_ms,
        "tokens": step.tokens_in + step.tokens_out, "tool_calls": [c["name"] for c in out.get("tool_calls", [])],
    }  # fmt: skip


# ------------------------------------------------------------------ model step


async def _model_step(run: Run, steps: list[Step], agent: AgentSpec, client, context: str = "") -> str | None:
    """Ask the model for its next move and record the answer. Returns a status if the run ended."""
    idx = len(steps)
    messages = build_messages(run.question, steps, agent.system_prompt, context)
    await publish(run.id, "step.started", {"idx": idx, "kind": "model"})
    span_args = dict(run_id=run.id, org_id=run.org_id, step=idx, **{tracing.PROVIDER: client.config.provider})
    try:
        with tracing.traced("model", client.config.model, **span_args) as span:
            response, cost = await client.complete(messages, agent.registry.specs(run.role))
            span.set_attribute(tracing.RESPONSE_MODEL, response.model)
            span.set_attribute(tracing.INPUT_TOKENS, response.usage.input_tokens)
            span.set_attribute(tracing.OUTPUT_TOKENS, response.usage.output_tokens)
            span.set_attribute(tracing.COST_USD, float(cost))
    except LLMError as exc:
        log.warning("model call failed", extra={"error": str(exc)})
        return await _end(run.org_id, run.id, "failed", error=f"The model call failed: {exc}")

    seen: set[str] = set()
    calls = []
    for n, call in enumerate(response.tool_calls):
        call_id = call.id if call.id and call.id not in seen else f"{call.id or 'call'}_{idx}_{n}"
        seen.add(call_id)
        calls.append(
            {"id": call_id, "name": call.name, "arguments": call.arguments, "invalid": call.invalid, "raw": call.raw}
        )
    step = Step(
        org_id=run.org_id, run_id=run.id, idx=idx, kind="model", name=response.model, state="done",
        input={"messages": len(messages)},
        # Stored as the model wrote it: the next step must execute exactly these arguments. Masking happens
        # where a step leaves the system (events, API responses), not here.
        output={"text": response.text, "tool_calls": calls, "stop_reason": response.stop_reason},
        tokens_in=response.usage.input_tokens, tokens_out=response.usage.output_tokens, cost_usd=cost,
        latency_ms=response.latency_ms, idempotency_key=_key(run.id, idx, "model", None), finished_at=_now(),
    )  # fmt: skip
    await _insert_step(
        run.org_id,
        step,
        run_values={
            "steps_used": Run.steps_used + 1,
            "tokens_used": Run.tokens_used + response.usage.input_tokens + response.usage.output_tokens,
            "cost_usd": Run.cost_usd + cost,
        },
    )
    await publish(run.id, "step.finished", _step_event(step))
    if calls:
        return None
    # The model answered in prose instead of acting. Remind it once; a second time ends the run.
    if steps and steps[-1].kind == "note":
        return await _end(run.org_id, run.id, "failed", error="The model stopped using tools before finishing.")
    note = Step(
        org_id=run.org_id, run_id=run.id, idx=idx + 1, kind="note", name="nudge", state="done",
        output={"text": NUDGE}, idempotency_key=_key(run.id, idx + 1, "note", None), finished_at=_now(),
    )  # fmt: skip
    await _insert_step(run.org_id, note)
    return None


# ------------------------------------------------------------------ tool steps


def _validate(agent: AgentSpec, role: str, call: dict) -> tuple[Tool | None, str | None]:
    tool = agent.registry.get(call["name"], role)
    if call.get("invalid"):
        return tool, f"{INVALID}the arguments were not a JSON object: {(call.get('raw') or '')[:200]}"
    if tool is None:
        names = ", ".join(s.name for s in agent.registry.specs(role))
        return None, f"{INVALID}there is no tool named '{call['name']}'. Available tools: {names}."
    try:
        tool.args_model.model_validate(call["arguments"])
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'arguments'}: {e['msg']}" for e in exc.errors()[:5])
        return tool, f"{INVALID}{problems}"
    return tool, None


async def _start_tool(run: Run, steps: list[Step], agent: AgentSpec, call: dict) -> str | None:
    idx = len(steps)
    tool, problem = _validate(agent, run.role, call)
    common = dict(
        org_id=run.org_id, run_id=run.id, idx=idx, kind="tool", name=call["name"], call_id=call["id"],
        input=call["arguments"], idempotency_key=_key(run.id, idx, call["name"], call["arguments"]),
    )  # fmt: skip

    if problem:
        step = Step(**common, state="done", error=problem, finished_at=_now())
        await _insert_step(run.org_id, step)
        await publish(run.id, "step.finished", _step_event(step))
        last_model = max(i for i, s in enumerate(steps) if s.kind == "model")
        earlier_tools = [s for s in steps[:last_model] if s.kind == "tool"]
        if earlier_tools and (earlier_tools[-1].error or "").startswith(INVALID):
            return await _end(run.org_id, run.id, "failed", error="The model called tools incorrectly twice in a row.")
        return None

    if tool.level == "needs_approval":
        step = Step(**common, state="awaiting_approval")
        approval = Approval(
            org_id=run.org_id, run_id=run.id, step_idx=idx, tool=tool.name, arguments=redact(call["arguments"]),
            requested_by=run.user_id,
        )  # fmt: skip
        pending = {"kind": "approval", "tool": tool.name, "step_idx": idx}
        await _insert_step(
            run.org_id, step, extra=approval, run_values={"status": "waiting_approval", "pending": pending}
        )
        await publish(run.id, "approval.requested", {"idx": idx, "tool": tool.name, "arguments": step.input})
        await publish(run.id, "run.status", {"status": "waiting_approval"})
        return "waiting_approval"

    step = Step(**common, state="started")
    await _insert_step(run.org_id, step)  # the intent is on record before anything is executed
    await publish(run.id, "step.started", {"idx": idx, "kind": "tool", "name": tool.name, "input": step.input})
    return await _execute(run, idx, tool)


async def _execute(run: Run, idx: int, tool: Tool) -> str | None:
    """Run the tool, then record its outcome and apply its database effects in one transaction."""
    started = time.perf_counter()
    result: ToolResult | None = None
    error: str | None = None
    async with tenant_session(run.org_id) as db:
        arguments, call_id = (
            await db.execute(select(Step.input, Step.call_id).where(Step.run_id == run.id, Step.idx == idx))
        ).one()
    span_args = dict(run_id=run.id, org_id=run.org_id, step=idx, **{tracing.TOOL_CALL_ID: call_id})
    try:
        args = tool.args_model.model_validate(arguments or {})
        ctx = ToolContext(run.id, run.org_id, run.user_id, run.role, idx)
        with tracing.traced("tool", tool.name, **span_args):
            result = await asyncio.wait_for(tool.fn(ctx, args), tool.timeout_s)
    except ToolError as exc:
        error = str(exc)
    except TimeoutError:
        error = f"The tool timed out after {tool.timeout_s:g} seconds."
    except ValidationError as exc:
        error = f"{INVALID}{exc.errors()[0]['msg']}"
    except Exception:  # noqa: BLE001 - a tool bug must not take the worker down
        log.exception("tool crashed", extra={"tool": tool.name})
        error = "The tool failed unexpectedly."
    latency = round((time.perf_counter() - started) * 1000)
    try:
        return await _record_outcome(run, idx, result, error, latency)
    except RunConflict:
        raise
    except Exception:  # noqa: BLE001 - e.g. the tool's database effects could not be applied
        log.exception("could not record tool outcome", extra={"tool": tool.name})
        return await _record_outcome(run, idx, None, "The tool's result could not be saved.", latency)


async def _record_outcome(run: Run, idx: int, result: ToolResult | None, error: str | None, latency: int) -> str | None:
    status: str | None = None
    async with tenant_session(run.org_id) as db:
        step = (
            await db.execute(select(Step).where(Step.run_id == run.id, Step.idx == idx).with_for_update())
        ).scalar_one()
        if step.state == "done":
            return None
        step.latency_ms, step.finished_at = latency, _now()
        if error is not None:
            step.error, step.state = error, "done"
        elif result.signal == "wait_user":
            step.state = "awaiting_user"
            pending = {"kind": "question", "question": result.summary, "step_idx": idx}
            await db.execute(update(Run).where(Run.id == run.id).values(status="waiting_user", pending=pending))
            status = "waiting_user"
        else:
            step.output = redact(
                {"summary": result.summary, "data": result.data, "handle": result.handle, "signal": result.signal}
            )
            step.state = "done"
            if result.commit is not None:
                await result.commit(db)
            if result.signal == "finish":
                await db.execute(
                    update(Run)
                    .where(Run.id == run.id)
                    .values(status="completed", result=result.data, finished_at=_now(), pending=None,
                            lease_owner=None, lease_expires_at=None)
                )  # fmt: skip
                status = "completed"
        event = _step_event(step)
    if status == "waiting_user":
        await publish(run.id, "question.asked", {"idx": idx, "question": result.summary})
    else:
        await publish(run.id, "step.finished", event)
    if status == "completed":
        await publish(run.id, "report.ready", {})
    if status:
        await publish(run.id, "run.status", {"status": status})
    return status


async def _resolve(run: Run, step: Step, agent: AgentSpec) -> str | None:
    """Deal with a tool call that was started but is not finished."""
    tool = agent.registry.get(step.name, run.role)
    if tool is None:
        return await _record_outcome(run, step.idx, None, "This tool is no longer available.", 0)
    if step.state == "awaiting_user":
        return "waiting_user"
    if step.state == "awaiting_approval":
        async with tenant_session(run.org_id) as db:
            approval = (
                await db.execute(select(Approval).where(Approval.run_id == run.id, Approval.step_idx == step.idx))
            ).scalar_one()
        if approval.status == "pending":
            return "waiting_approval"
        if approval.status == "rejected":
            reason = f": {approval.reason}" if approval.reason else "."
            return await _record_outcome(run, step.idx, None, f"An administrator rejected this action{reason}", 0)
        return await _execute(run, step.idx, tool)
    # state == "started": a worker died while this tool was running.
    if tool.idempotent:
        return await _execute(run, step.idx, tool)
    return await _record_outcome(
        run, step.idx, None,
        "This action was interrupted and may or may not have taken effect. Check before trying it again.", 0,
    )  # fmt: skip


# ------------------------------------------------------------------ the loop


async def advance(run_id: UUID, agent: AgentSpec, *, worker_id: str | None = None) -> str:
    """Move a run forward until it finishes or has to wait. Returns its status.

    Safe to call again at any time: on a finished or waiting run it does nothing.
    """
    async with system_session() as db:
        org_id = await db.scalar(select(Run.org_id).where(Run.id == run_id))
    if org_id is None:
        return "missing"
    token = run_id_var.set(str(run_id))
    began = time.perf_counter()
    client = None
    context: str | None = None
    try:
        for _ in range(MAX_ITERATIONS):
            run, steps = await _load(org_id, run_id)
            if run.status != "running":
                return run.status
            if worker_id is not None and run.lease_owner != worker_id:
                raise RunConflict(f"run {run_id} is now leased to {run.lease_owner}")
            if run.cancel_requested:
                return await _end(org_id, run_id, "cancelled")
            action = next_action(steps)
            if action.kind == "model":
                # Budgets are checked before each model call, the only step that spends money. Tool calls the
                # model has already asked for are allowed to finish, so no request is left half-answered.
                try:
                    await check_budget(run, round((time.perf_counter() - began) * 1000))
                except BudgetExceeded as exc:
                    return await _end(org_id, run_id, "budget_exceeded", error=str(exc))
                try:
                    client = client or await factory.get_client(org_id)
                except ValueError as exc:  # a settings problem, not a crash: say so instead of retrying
                    return await _end(org_id, run_id, "failed", error=f"The model is not configured correctly: {exc}")
                if context is None:
                    context = await agent.context(org_id) if agent.context else ""
                status = await _model_step(run, steps, agent, client, context)
            elif action.kind == "start_tool":
                status = await _start_tool(run, steps, agent, action.call)
            else:
                status = await _resolve(run, action.step, agent)
            if status in TERMINAL or status in ("waiting_user", "waiting_approval"):
                if status != run.status and status in ("waiting_user", "waiting_approval"):
                    async with tenant_session(org_id) as db:
                        await db.execute(update(Run).where(Run.id == run_id).values(status=status))
                return status
        return await _end(org_id, run_id, "failed", error="The run did not converge.")
    finally:
        elapsed = round((time.perf_counter() - began) * 1000)
        async with tenant_session(org_id) as db:
            await db.execute(update(Run).where(Run.id == run_id).values(active_ms=Run.active_ms + elapsed))
        run_id_var.reset(token)
