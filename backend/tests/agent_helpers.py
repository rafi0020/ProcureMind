"""A small toy agent and helpers for exercising the runtime without any finance code."""

import asyncio
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select, text, update

from app.agent.tools import AgentSpec, ToolContext, ToolError, ToolRegistry, ToolResult
from app.db import tenant_session
from app.llm.stub import reply
from app.llm.types import ToolCall
from app.models.audit import AuditLog
from app.models.runs import Approval, Run, Step

registry = ToolRegistry()
effects: list[str] = []  # stands in for an effect outside the database


class Text(BaseModel):
    text: str


class Nothing(BaseModel):
    pass


@registry.tool("echo", "Repeat the text.")
async def echo(ctx: ToolContext, args: Text) -> ToolResult:
    return ToolResult(f"echo {args.text}", data={"text": args.text})


@registry.tool("boom", "Always crashes.")
async def boom(ctx: ToolContext, args: Nothing) -> ToolResult:
    raise RuntimeError("secret internal detail")


@registry.tool("refuse", "Always fails with a message for the model.")
async def refuse(ctx: ToolContext, args: Nothing) -> ToolResult:
    raise ToolError("there is no such thing")


@registry.tool("slow", "Takes too long.", timeout_s=0.2)
async def slow(ctx: ToolContext, args: Nothing) -> ToolResult:
    await asyncio.sleep(2)
    return ToolResult("never")


@registry.tool("big", "Returns a large result.")
async def big(ctx: ToolContext, args: Nothing) -> ToolResult:
    return ToolResult("500 rows", data={"rows": [[i, "x" * 20] for i in range(500)]}, handle="ev_big00001")


@registry.tool("leak", "Returns something secret-looking.")
async def leak(ctx: ToolContext, args: Nothing) -> ToolResult:
    return ToolResult("found sk-abcdefghijklmnopqrstuv in a file", data={"api_key": "hunter2", "value": 1})


@registry.tool("note", "Write an audit entry.", level="write", idempotent=True)
async def note(ctx: ToolContext, args: Text) -> ToolResult:
    async def commit(db):
        db.add(AuditLog(org_id=ctx.org_id, user_id=ctx.user_id, action="toy.note", target=args.text))

    return ToolResult(f"noted {args.text}", commit=commit)


@registry.tool("launch", "Do something consequential.", level="needs_approval", roles=("clerk", "admin"))
async def launch(ctx: ToolContext, args: Text) -> ToolResult:
    effects.append(args.text)
    return ToolResult(f"launched {args.text}")


@registry.tool("admin_only", "Only administrators.", roles=("admin",))
async def admin_only(ctx: ToolContext, args: Nothing) -> ToolResult:
    return ToolResult("ok")


@registry.tool("ask_user", "Ask the user a question.")
async def ask_user(ctx: ToolContext, args: Text) -> ToolResult:
    return ToolResult(args.text, signal="wait_user")


@registry.tool("finish", "Give the final answer.")
async def finish(ctx: ToolContext, args: Text) -> ToolResult:
    return ToolResult("finished", data={"summary": args.text}, signal="finish")


TOY = AgentSpec("You are a test agent.", registry)


def call(name: str, call_id: str = "", **arguments):
    """A scripted model turn that calls one tool."""
    return reply(tool_calls=[ToolCall(call_id or f"c_{name}", name, arguments)])


def done(text: str = "all done"):
    return call("finish", text=text)


async def new_run(
    team, question: str = "test question", role: str = "clerk", status: str = "running", exception_id=None
) -> UUID:
    async with tenant_session(UUID(team.org_id)) as db:
        run = Run(
            org_id=UUID(team.org_id),
            user_id=UUID(team.user_ids[role]),
            role=role,
            question=question,
            status=status,
            exception_id=UUID(exception_id) if exception_id else None,
        )
        db.add(run)
        await db.flush()
        return run.id


async def get_run(team, run_id) -> Run:
    async with tenant_session(UUID(team.org_id)) as db:
        return (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()


async def get_steps(team, run_id) -> list[Step]:
    async with tenant_session(UUID(team.org_id)) as db:
        return list((await db.execute(select(Step).where(Step.run_id == run_id).order_by(Step.idx))).scalars())


async def decide(team, run_id, decision: str, reason: str | None = None) -> None:
    """What the approvals API does: record the decision and put the run back in play."""
    async with tenant_session(UUID(team.org_id)) as db:
        await db.execute(
            update(Approval).where(Approval.run_id == run_id).values(status=decision, reason=reason,
                                                                      decided_by=UUID(team.user_ids["admin"]))
        )  # fmt: skip
        await db.execute(update(Run).where(Run.id == run_id).values(status="running", pending=None))


async def audit_targets(team, action: str) -> list[str]:
    async with tenant_session(UUID(team.org_id)) as db:
        rows = await db.execute(text("SELECT target FROM audit_log WHERE action = :a ORDER BY id"), {"a": action})
        return [r[0] for r in rows]


# ---- tools and a scripted model for tests that run a real worker process


def _effect_file() -> str:
    import os

    return os.environ["TOY_EFFECT_FILE"]


async def _touch_then_sleep(label: str) -> None:
    import os

    with open(_effect_file(), "a") as f:
        f.write(label + "\n")
    await asyncio.sleep(float(os.environ.get("TOY_SLEEP", "0")))


@registry.tool("outside_effect", "Changes something outside the database, then takes a while.", level="write")
async def outside_effect(ctx: ToolContext, args: Nothing) -> ToolResult:
    await _touch_then_sleep("write")
    return ToolResult("effect applied")


@registry.tool("slow_lookup", "A read that takes a while.")
async def slow_lookup(ctx: ToolContext, args: Nothing) -> ToolResult:
    await _touch_then_sleep("read")
    return ToolResult("looked up")


def process_responder(messages, tools):
    """Model behaviour for the worker subprocess: call the tool named in the question once, then finish."""
    question = next(m.content for m in messages if m.role == "user")
    if not any(m.role == "tool" for m in messages):
        return call(question)
    return done(messages[-1].content[:200])


async def work(agent=TOY, worker: str = "test-worker") -> int:
    """Do what a worker does until the queue is empty. Returns how many runs it advanced."""
    from app.worker.run import claim_next, run_one

    n = 0
    while (run_id := await claim_next(worker)) is not None:
        await run_one(run_id, agent, worker, lease_s=30)
        n += 1
    return n


def sse_events(body: str) -> list[dict]:
    """Parse a Server-Sent Events body into [{id, event, data}]."""
    import json

    events = []
    for block in body.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(":"))
        if "event" in fields:
            events.append({"id": fields["id"], "event": fields["event"], "data": json.loads(fields["data"])})
    return events
