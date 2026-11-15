"""Recorded evaluation results, for the Evaluation screen. The files are produced by the scripts in eval/
(extraction, matching, the agent's cases, attacks) and shipped with the application; nothing here runs a model.

A run in which the provider never answered is listed apart, under `unavailable`, so a provider outage is not
shown as the model's score."""

import json
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends

from app.api.deps import current_user
from app.security.sessions import Principal

router = APIRouter(prefix="/api/eval", tags=["evaluation"])
RESULTS = Path(__file__).resolve().parents[2] / "eval" / "results"
SHOWN = ("name", "model", "split", "at", "n", "metrics", "by_kind", "note", "regraded_at")
EXTRACTION = (
    "name",
    "mode",
    "model",
    "seed",
    "at",
    "documents",
    "field_accuracy",
    "invented",
    "by_quality",
    "by_field",
)
MATCHING = (
    "name",
    "scale",
    "scans",
    "at",
    "documents",
    "status_accuracy",
    "findings_precision",
    "findings_recall",
    "per_code",
)


@lru_cache(maxsize=1)
def _load(signature: tuple) -> dict:
    results, attacks, unavailable, extraction, matching = [], [], [], [], []
    for path in sorted(RESULTS.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if path.name.startswith("extraction-"):
            extraction.append({k: data[k] for k in EXTRACTION if k in data})
        elif path.name.startswith("matching-"):
            matching.append({k: data[k] for k in MATCHING if k in data})
        elif "cases" in data:
            attacks.append({k: data[k] for k in ("model", "at", "cases", "note") if k in data})
        elif data.get("provider_unavailable"):
            unavailable.append({k: data.get(k) for k in ("name", "model", "split", "at", "n", "note")})
        elif "metrics" in data:
            results.append({k: data[k] for k in SHOWN if k in data})
    return {
        "results": results,
        "attacks": attacks,
        "unavailable": unavailable,
        "extraction": extraction,
        "matching": matching,
    }


@router.get("/results")
async def results(_: Principal = Depends(current_user)) -> dict:
    signature = (
        tuple((p.name, p.stat().st_mtime_ns) for p in sorted(RESULTS.glob("*.json"))) if RESULTS.exists() else ()
    )
    return _load(signature)
