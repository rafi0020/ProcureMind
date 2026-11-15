"""Rebuilds the conversation sent to the model from the stored steps.

Nothing about a run lives in memory between steps. The message list is derived from the `steps` table every
time, which is why a run can continue on a different worker after a crash, an approval or a clarifying answer.

Context is kept small on purpose: a large tool result is not replayed in full; the model sees its summary and
a handle it can pass to other tools.
"""

import json

from app.llm.types import Message, ToolCall
from app.models.runs import Step

MAX_TOOL_OUTPUT_CHARS = 4000


def render_tool_output(step: Step) -> str:
    if step.error:
        return f"ERROR: {step.error}"
    out = step.output or {}
    body = {"summary": out.get("summary", "")}
    if out.get("handle"):
        body["handle"] = out["handle"]
    if out.get("data") is not None:
        encoded = json.dumps(out["data"], default=str)
        if len(encoded) <= MAX_TOOL_OUTPUT_CHARS:
            body["data"] = out["data"]
        else:
            body["note"] = "Result too large to show in full; use the handle with other tools, or narrow the request."
    return json.dumps(body, default=str)


def build_messages(question: str, steps: list[Step], system_prompt: str, context: str = "") -> list[Message]:
    """Only finished steps are replayed; a tool call still waiting has no result to show yet."""
    first = f"{context}\n\nQuestion: {question}" if context else question
    messages = [Message("system", system_prompt), Message("user", first)]
    for step in steps:
        if step.kind == "model":
            out = step.output or {}
            calls = [
                ToolCall(c["id"], c["name"], c.get("arguments") or {}, bool(c.get("invalid")), c.get("raw"))
                for c in out.get("tool_calls", [])
            ]
            messages.append(Message("assistant", out.get("text", ""), calls))
        elif step.kind == "tool" and step.state == "done":
            messages.append(Message("tool", render_tool_output(step), tool_call_id=step.call_id))
        elif step.kind == "note":
            messages.append(Message("user", (step.output or {}).get("text", "")))
    return messages
