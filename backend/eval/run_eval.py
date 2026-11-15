"""Runs the Exception Agent on the evaluation cases with a real model and grades what it recommended.

    python -m eval.run_eval --split dev --model cc/claude-sonnet-5-5
    python -m eval.run_eval --rescore eval/results/dev-cc-claude-sonnet-5-5.json

Graded from the database, not from the agent's own account:
  outcome  the recommendation stored on the exception is one an AP lead would accept for this kind of case
  cited    where the case has a deciding figure (the variance, the quantity received, the duplicate's total, the limit),
           the report states it
  pass     both
Unsupported figures (numbers in the report not found in any evidence) are counted per report.
"""

import argparse
import asyncio
import json
import statistics
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text

from app.api.exceptions import question_for
from app.db import dispose_engines, tenant_session
from app.models.identity import User
from app.models.runs import Run, Step
from app.redis import close_redis
from eval.build_questions import OSPREY
from eval.common import ask, configure_model, number_matches, report_text

HERE = Path(__file__).parent


def grade(question: dict, recommendation: dict | None, report: dict | None, status: str) -> dict:
    outcome = (recommendation or {}).get("outcome")
    text_ = report_text(report) + " " + (recommendation or {}).get("rationale", "")
    outcome_ok = outcome in question["acceptable"]
    cite = question.get("cite")
    cited = number_matches(text_, cite["value"], count=question["kind"] == "quantity_over_received") if cite else None
    passed = outcome_ok and cited is not False
    if status != "completed":
        reason = f"run ended {status}"
    elif not outcome:
        reason = "no recommendation stored"
    elif not outcome_ok:
        reason = f"recommended {outcome}; acceptable {question['acceptable']}"
    elif cited is False:
        reason = f"did not state {cite['what']}"
    else:
        reason = f"recommended {outcome}" + (f", stated {cite['what']}" if cite else "")
    return {"outcome": outcome, "outcome_ok": outcome_ok, "cited": cited, "passed": passed, "reason": reason}


async def recommendation_of(run_id: UUID) -> dict | None:
    """The last recommendation this run stored: its own successful `recommend` call, so a later run on the same
    exception (another model, say) never changes what this one is graded on."""
    async with tenant_session(OSPREY) as db:
        step = (await db.execute(select(Step).where(Step.run_id == run_id, Step.name == "recommend", Step.error.is_(None))
                                 .order_by(Step.idx.desc()).limit(1))).scalar_one_or_none()  # fmt: skip
    return dict(step.input) if step and step.input else None


async def reopen(exception_id: str) -> None:
    """A case is graded on a fresh exception: no earlier recommendation, status open."""
    async with tenant_session(OSPREY) as db:
        await db.execute(
            text("UPDATE exceptions SET recommendation = NULL, status = 'open' WHERE id = :e"),
            {"e": UUID(exception_id)},
        )
        await db.execute(text("DELETE FROM drafts WHERE exception_id = :e"), {"e": UUID(exception_id)})


async def run_case(q: dict, clerk: UUID, gate: asyncio.Semaphore) -> dict:
    async with gate:
        await reopen(q["exception_id"])
        o = await ask(OSPREY, clerk, question_for(q["exception_id"]), exception_id=UUID(q["exception_id"]))
    g = grade(q, await recommendation_of(o.run.id), o.run.result, o.run.status)
    print(f"  {q['id']} {'PASS' if g['passed'] else 'FAIL'} -> {g['reason']}")
    return {"id": q["id"], "kind": q["kind"], **g, "status": o.run.status, "steps": o.run.steps_used, "tokens": o.run.tokens_used,
            "cost_usd": float(o.run.cost_usd), "latency_s": round(o.seconds, 1), "unsupported": len((o.run.result or {}).get("unsupported", [])),
            "tool_errors": sum(1 for s in o.steps if s.kind == "tool" and s.error), "run_id": str(o.run.id),
            "summary": ((o.run.result or {}).get("summary") or o.run.error or "")[:400]}  # fmt: skip


def aggregate(rows: list[dict]) -> tuple[dict, dict]:
    latencies = sorted(r["latency_s"] for r in rows)
    cites = [r for r in rows if r["cited"] is not None]
    completed = [r for r in rows if r["status"] == "completed"]
    metrics = {
        "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 4),
        "outcome_accuracy": round(sum(r["outcome_ok"] for r in rows) / len(rows), 4),
        "cite_rate": round(sum(bool(r["cited"]) for r in cites) / len(cites), 4) if cites else None,
        "completed_rate": round(len(completed) / len(rows), 4),
        "unsupported_numbers_per_report": round(sum(r["unsupported"] for r in completed) / max(1, len(completed)), 3),
        "mean_steps": round(statistics.mean(r["steps"] for r in rows), 2),
        "mean_cost_usd": round(statistics.mean(r["cost_usd"] for r in rows), 4),
        "p50_latency_s": latencies[len(latencies) // 2],
        "p95_latency_s": latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))],
    }
    by_kind = {
        k: {"n": sum(r["kind"] == k for r in rows), "passed": sum(r["passed"] for r in rows if r["kind"] == k)}
        for k in dict.fromkeys(r["kind"] for r in rows)
    }
    return metrics, by_kind


async def rescore(path: Path) -> None:
    """Grade stored runs again with the current grader; no model is called, so only the marking can change."""
    result = json.loads(await asyncio.to_thread(path.read_text))
    questions = {
        q["id"]: q for q in json.loads(await asyncio.to_thread((HERE / f"questions_{result['split']}.json").read_text))
    }
    for row in result["questions"]:
        async with tenant_session(OSPREY) as db:
            run = (await db.execute(select(Run).where(Run.id == UUID(row["run_id"])))).scalar_one()
        row.update(grade(questions[row["id"]], await recommendation_of(run.id), run.result, run.status))
    result["metrics"], result["by_kind"] = aggregate(result["questions"])
    result["regraded_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    await asyncio.to_thread(path.write_text, json.dumps(result, indent=1))
    print(json.dumps(result["metrics"], indent=1))


async def main(args) -> None:
    if args.rescore:
        await rescore(Path(args.rescore))
    else:
        questions = json.loads((HERE / f"questions_{args.split}.json").read_text())
        if args.only:
            questions = [q for q in questions if q["id"] in args.only.split(",") or q["kind"] in args.only.split(",")]
        configure_model(args.model)
        async with tenant_session(OSPREY) as db:
            clerk = (await db.execute(select(User.id).where(User.role == "clerk"))).scalar_one()
        print(f"{len(questions)} cases, split={args.split}, model={args.model}")
        gate = asyncio.Semaphore(args.concurrency)
        rows = await asyncio.gather(*[run_case(q, clerk, gate) for q in questions])
        metrics, by_kind = aggregate(rows)
        print(json.dumps(metrics, indent=1), by_kind)
        name = f"{args.split}-{args.model.replace('/', '-')}"
        result = {"name": name, "model": args.model, "split": args.split, "n": len(rows), "at": datetime.now(UTC).isoformat(timespec="seconds"),
                  "metrics": metrics, "by_kind": by_kind, "questions": rows}  # fmt: skip
        (HERE / "results" / f"{name}.json").write_text(json.dumps(result, indent=1))
    await close_redis()
    await dispose_engines()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate the Exception Agent")
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--model", default="")
    parser.add_argument("--rescore", default="")
    parser.add_argument("--only", default="")
    parser.add_argument("--concurrency", type=int, default=3)
    asyncio.run(main(parser.parse_args()))
