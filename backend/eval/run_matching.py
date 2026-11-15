"""Match statuses and check findings against the demo's answer key.

    python -m eval.run_matching --scale 0.1          # in the tests container; uses the TEST database

It deletes the two demo organisations from the database it is pointed at, generates the demo afresh with its answer
key (scans included, so OCR mistakes reach the matcher as they would in use), processes every document through the
real pipeline, and compares:
  status     each document's invoice ended in the state the key gives (waiting for approval, exception, applied)
  findings   per check (duplicates, bank change, split under a limit, price, quantity, no PO): precision and recall of
             the reasons and findings on the invoice's exception against the key's planted findings. Unplanted
             documents expect none, so any finding on them is a false positive.
Never point it at a database whose demo organisations you want to keep.
"""

import argparse
import asyncio
import json
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from app.config import get_settings
from app.db import dispose_engines, system_session, tenant_session
from app.intake.queue import process_pending
from app.redis import close_redis
from seed.demo_users import KESTREL, RIDGEWAY
from seed.generate import generate

HERE = Path(__file__).parent


async def fresh_demo(scale: float, scans: bool) -> dict:
    async with system_session() as db:
        await db.execute(text("DELETE FROM orgs WHERE id = ANY(:o)"), {"o": [KESTREL, RIDGEWAY]})
    key = Path(tempfile.mkdtemp()) / "truth.json"
    await generate(seed=7, scale=scale, scans=scans, truth_path=key)
    await process_pending("evaluation")
    return json.loads(key.read_text())


async def outcomes() -> dict[str, dict]:
    got = {}
    for org in (KESTREL, RIDGEWAY):
        async with tenant_session(org) as db:
            rows = await db.execute(
                text("""
                SELECT d.filename, d.status AS doc_status, i.status, e.reasons, e.findings
                FROM documents d LEFT JOIN invoices i ON i.document_id = d.id
                LEFT JOIN exceptions e ON e.invoice_id = i.id AND e.status <> 'resolved'""")
            )
            for r in rows:
                codes = {x["code"] for x in (r.reasons or [])} | {x["code"] for x in (r.findings or [])}
                got[r.filename] = {"document": r.doc_status, "status": r.status, "codes": sorted(codes)}
    return got


def score(truth: dict, got: dict) -> dict:
    tp, fp, fn = Counter(), Counter(), Counter()
    status_ok, wrong = 0, []
    for name, want in truth["documents"].items():
        have = got.get(name, {"status": None, "codes": [], "document": "missing"})
        if have["status"] == want["invoice_status"]:
            status_ok += 1
        else:
            wrong.append({"document": name, "expected": want["invoice_status"], "got": have["status"], "document_status": have["document"],
                          "codes": have["codes"], "quality": want.get("quality")})  # fmt: skip
        expected, found = set(want.get("findings", [])), set(have["codes"])
        for code in found & expected:
            tp[code] += 1
        for code in found - expected:
            fp[code] += 1
        for code in expected - found:
            fn[code] += 1
    codes = sorted(set(tp) | set(fp) | set(fn))
    per_code = {c: {"tp": tp[c], "fp": fp[c], "fn": fn[c], "precision": round(tp[c] / (tp[c] + fp[c]), 4) if tp[c] + fp[c] else None,
                    "recall": round(tp[c] / (tp[c] + fn[c]), 4) if tp[c] + fn[c] else None} for c in codes}  # fmt: skip
    t, f, n = sum(tp.values()), sum(fp.values()), sum(fn.values())
    return {"documents": len(truth["documents"]), "status_accuracy": round(status_ok / len(truth["documents"]), 4),
            "findings_precision": round(t / (t + f), 4) if t + f else None, "findings_recall": round(t / (t + n), 4) if t + n else None,
            "per_code": per_code, "wrong_status": wrong}  # fmt: skip


async def main(args) -> None:
    if "_test" not in get_settings().database_url and not args.force:
        raise SystemExit("This deletes the demo organisations. Point it at the test database, or pass --force.")
    truth = await fresh_demo(args.scale, not args.no_scans)
    result = {"name": f"matching-scale{args.scale}", "scale": args.scale, "scans": not args.no_scans, "at": datetime.now(UTC).isoformat(timespec="seconds"),
              **score(truth, await outcomes())}  # fmt: skip
    print(json.dumps({k: v for k, v in result.items() if k != "wrong_status"}, indent=1))
    print(
        f"{len(result['wrong_status'])} documents ended in another state:",
        json.dumps(result["wrong_status"][:15], indent=1),
    )
    (HERE / "results" / f"{result['name']}.json").write_text(json.dumps(result, indent=1))
    await close_redis()
    await dispose_engines()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Matching and checks against the answer key")
    parser.add_argument("--scale", type=float, default=0.1)
    parser.add_argument("--no-scans", action="store_true")
    parser.add_argument("--force", action="store_true")
    asyncio.run(main(parser.parse_args()))
