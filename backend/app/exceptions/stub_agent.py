"""What LLM_PROVIDER=stub does: a fixed script, so the whole flow works with no model and says it is a script."""

import re

from app.llm.stub import reply
from app.llm.types import LLMResponse, Message, ToolCall, ToolSpec

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
SCRIPT = "This is the built-in demonstration script, not a language model"


def demo_responder(messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
    if not tools:
        return reply(SCRIPT + ".")
    step = sum(len(m.tool_calls) for m in messages if m.role == "assistant")
    question = next((m.content for m in messages if m.role == "user"), "")
    tool_text = " ".join(m.content for m in messages if m.role == "tool")
    handles = re.findall(r"ev_[0-9a-f]{8}", tool_text)
    exception = next(iter(re.findall(r"exception (" + UUID + ")", question, re.I)), None)
    invoice = next(iter(re.findall(r'"invoice_id": "(' + UUID + ')"', tool_text)), None)

    def call(name, **arguments):
        return reply(tool_calls=[ToolCall(f"c{step}", name, arguments)])

    if exception is None:
        return call("finish", outcome="cannot_answer", summary=f"{SCRIPT}. Start it from an exception.")
    if step == 0:
        return call("get_exception", exception_id=exception)
    if step == 1 and invoice:
        return call("compare_lines", invoice_id=invoice)
    if step == 2 and handles:
        return call(
            "record_finding",
            claim="The exception's reasons and the line comparison were read from the records.",
            evidence=handles[-1:],
        )
    if step == 3 and handles:
        return call(
            "recommend",
            exception_id=exception,
            outcome="hold",
            rationale=f"{SCRIPT}: it always recommends a hold.",
            evidence=handles[-1:],
        )
    return call(
        "finish",
        summary=f"{SCRIPT}. It read the exception and the line comparison and recommended a hold.",
        findings=[1] if handles else [],
    )
