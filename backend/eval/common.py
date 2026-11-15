"""What every evaluation script shares: the model under test, running the agent on a question, reading a report."""

import os
import re
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from app.agent.runtime import advance
from app.config import get_settings
from app.db import tenant_session
from app.exceptions.agent import AGENT
from app.exceptions.grounding import extract_numbers
from app.llm import factory
from app.llm.openai_compat import OpenAICompatProvider
from app.models.runs import Evidence, Run, Step

EVAL_WORKER = "evaluation"


def configure_model(model: str) -> None:
    """Point every model call at `model` through an OpenAI-compatible endpoint: EVAL_BASE_URL / EVAL_API_KEY, or the
    local gateway's NINEROUTER_BASE_URL / NINEROUTER_API_KEY. Whatever an organisation saved is ignored."""
    base = os.environ.get("EVAL_BASE_URL") or os.environ["NINEROUTER_BASE_URL"]
    key = os.environ.get("EVAL_API_KEY") or os.environ["NINEROUTER_API_KEY"]
    settings = get_settings()
    settings.llm_provider, settings.llm_model = "openai_compat", model
    settings.org_daily_cost_usd, settings.run_max_seconds = 10_000.0, 420
    factory.set_override(OpenAICompatProvider(base, key))

    async def evaluation_config(org_id):
        return factory.environment_config()

    factory.load_config = evaluation_config


def report_text(report: dict | None) -> str:
    if not report:
        return ""
    return " ".join([report.get("summary", ""), *[f.get("claim", "") for f in report.get("findings", [])]])


def number_matches(text: str, expected: float, tolerance: float = 0.005, *, count: bool = False) -> bool:
    """A figure in the text equals `expected` to the precision it was printed with, or within `tolerance`.
    With count=True a whole quantity counts on its own ("only 6 received"), but not inside a code such as GRN-E0006;
    the report grounding skips small bare counts, which is right for checking reports and wrong for grading them."""
    if count and expected == int(expected) and re.search(rf"(?<![\w.,-]){int(expected)}(?![\w]|[.,]\d)", text):
        return True
    for mention in extract_numbers(text):
        for value, printed in mention.candidates:
            if abs(abs(value) - abs(expected)) <= max(printed, tolerance * abs(expected)):
                return True
    return False


class Outcome:
    def __init__(self, run: Run, steps: list[Step], evidence: list[Evidence], seconds: float):
        self.run, self.steps, self.evidence, self.seconds = run, steps, evidence, seconds
        self.text = report_text(run.result)
        self.tools = [s.name for s in steps if s.kind == "tool"]


async def ask(
    org_id: UUID, user_id: UUID, question: str, *, role: str = "clerk", exception_id: UUID | None = None
) -> Outcome:
    """Run the Exception Agent on one question, in this process, and read back everything it did."""
    async with tenant_session(org_id) as db:
        run = Run(org_id=org_id, user_id=user_id, role=role, question=question, status="running", lease_owner=EVAL_WORKER,
                  exception_id=exception_id,
                  lease_expires_at=datetime.now(UTC) + timedelta(hours=2))  # fmt: skip
        db.add(run)
        await db.flush()
        run_id = run.id
    started = time.perf_counter()
    try:
        await advance(run_id, AGENT, worker_id=EVAL_WORKER)
    except Exception as exc:  # noqa: BLE001 - one broken case must not stop the evaluation
        print(f"  run {run_id} crashed: {type(exc).__name__}: {exc}")
    seconds = time.perf_counter() - started
    async with tenant_session(org_id) as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        steps = list((await db.execute(select(Step).where(Step.run_id == run_id).order_by(Step.idx))).scalars())
        evidence = list((await db.execute(select(Evidence).where(Evidence.run_id == run_id))).scalars())
    return Outcome(run, steps, evidence, seconds)
