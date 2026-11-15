"""Spans for model and tool calls carry the GenAI attributes and never the content."""

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.agent.runtime import advance
from app.config import get_settings
from app.obs import tracing
from tests.agent_helpers import TOY, call, done, new_run


async def test_one_span_per_step_with_genai_attributes_and_no_content(team, stub_llm):
    exporter = InMemorySpanExporter()
    tracing.configure_tracing(exporter)
    try:
        stub_llm.script = [call("leak"), call("refuse"), done("the secret answer")]
        run_id = await new_run(team, question="a confidential question")
        await advance(run_id, TOY)
    finally:
        tracing.configure_tracing()
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == [
        "chat stub", "execute_tool leak", "chat stub", "execute_tool refuse", "chat stub", "execute_tool finish",
    ]  # fmt: skip
    assert {s.context.trace_id for s in spans} == {run_id.int}  # the whole run is one trace

    chat, tool = spans[0].attributes, spans[1].attributes
    assert chat["gen_ai.operation.name"] == "chat" and chat["gen_ai.provider.name"] == "stub"
    assert chat["gen_ai.request.model"] == "stub" and chat["gen_ai.response.model"] == "stub"
    assert chat["gen_ai.usage.input_tokens"] == 100 and chat["gen_ai.usage.output_tokens"] == 20
    assert (
        chat["procuremind.run.id"] == str(run_id)
        and chat["procuremind.org.id"] == team.org_id
        and chat["procuremind.step.index"] == 0
    )
    assert tool["gen_ai.operation.name"] == "execute_tool" and tool["gen_ai.tool.name"] == "leak"
    assert tool["gen_ai.tool.call.id"] == "c_leak"
    assert spans[3].status.status_code.name == "ERROR"  # the tool that refused

    everything = " ".join(f"{k}={v}" for s in spans for k, v in s.attributes.items())
    for secret in ("confidential question", "secret answer", "hunter2", "sk-abcdefghij"):
        assert secret not in everything


async def test_metrics_endpoint_reports_requests_and_runs(client, team, stub_llm, monkeypatch):
    stub_llm.script = [done()]
    await advance(await new_run(team), TOY)
    await client.get("/api/auth/me")
    body = (await client.get("/metrics")).text
    assert 'procuremind_runs{status="completed"}' in body and "procuremind_cost_usd_total" in body
    assert 'http_request_duration_seconds_count{method="GET",route="/api/auth/me",status="401"}' in body
    assert "/api/investigations/{run_id}" not in body or "run_id}" in body  # templates, never raw ids

    monkeypatch.setattr(get_settings(), "metrics_token", "scrape-me")
    assert (await client.get("/metrics")).status_code == 401
    assert (await client.get("/metrics", headers={"Authorization": "Bearer scrape-me"})).status_code == 200
