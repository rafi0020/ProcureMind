"""The Exception Agent with a real model on a planted case (needs NINEROUTER_API_KEY)."""

import os

import pytest

from app.agent.runtime import advance
from app.exceptions.agent import AGENT
from tests.agent_helpers import get_run, new_run
from tests.conftest import rows
from tests.test_exception_tools import planted_invoice

pytestmark = pytest.mark.live


async def test_price_over_tolerance_gets_a_credit_note_or_hold_with_the_variance_cited(kestrel):
    from app.llm import factory
    from app.llm.openai_compat import OpenAICompatProvider

    factory.set_override(OpenAICompatProvider(os.environ["NINEROUTER_BASE_URL"], os.environ["NINEROUTER_API_KEY"]))
    get_settings = __import__("app.config", fromlist=["get_settings"]).get_settings
    settings = get_settings()
    original = settings.llm_provider, settings.llm_model
    settings.llm_provider, settings.llm_model = "openai_compat", os.environ.get("LIVE_MODEL", "cc/claude-sonnet-5-5")
    try:
        _, exception = await planted_invoice(kestrel, "price_over_tolerance")
        run_id = await new_run(
            kestrel,
            question=f"[Exception {exception}] Investigate this exception and recommend an outcome.",
            role="clerk",
        )
        status = await advance(run_id, AGENT)
    finally:
        factory.set_override(None)
        settings.llm_provider, settings.llm_model = original
    run = await get_run(kestrel, run_id)
    assert status == "completed", run.error
    outcome = (
        await rows(kestrel.org_id, "SELECT recommendation->>'outcome' FROM exceptions WHERE id = :e", e=exception)
    )[0][0]
    assert outcome in ("request_credit_note", "hold")
    assert run.result["unsupported"] == [] and "10" in run.result["summary"]  # the 10.00 price difference
