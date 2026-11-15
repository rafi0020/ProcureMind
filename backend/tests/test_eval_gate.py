"""Regression gate on the recorded evaluation. A stored result must not fall below its baseline, extraction must
never store a value that is not on the page, recorded attacks must hold, and the question sets must stay apart.
A result with no baseline fails, so a new model or mode is measured before it is relied on."""

import json
from pathlib import Path

import pytest

EVAL = Path(__file__).resolve().parents[1] / "eval"
BASELINE = json.loads((EVAL / "baseline.json").read_text())


def results(prefix: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((EVAL / "results").glob(f"{prefix}*.json"))]


AGENT = results("test-")
EXTRACTION = results("extraction-")
MATCHING = results("matching-")
ATTACKS = results("attacks-")


def test_a_held_out_agent_result_has_been_recorded_for_two_models():
    assert len({r["model"] for r in AGENT}) >= 2, "run: make eval-agent SPLIT=test MODEL=<model>"


@pytest.mark.parametrize("result", AGENT, ids=[r["name"] for r in AGENT])
def test_held_out_agent_result_is_within_tolerance_of_its_baseline(result):
    floors = BASELINE["agent"].get(result["name"])
    assert floors, f"{result['name']} has no baseline in eval/baseline.json"
    for metric, floor in floors.items():
        assert result["metrics"][metric] >= floor - 0.05, f"{result['name']}: {metric} fell below the baseline"


@pytest.mark.parametrize("result", EXTRACTION, ids=[r["name"] for r in EXTRACTION])
def test_extraction_never_stores_a_value_that_is_not_on_the_page(result):
    assert result["invented"] == 0
    floor = BASELINE["extraction"].get(result["name"])
    assert floor is not None, f"{result['name']} has no baseline"
    assert result["field_accuracy"] >= floor - 0.02


@pytest.mark.parametrize("result", MATCHING, ids=[r["name"] for r in MATCHING])
def test_matching_holds_its_status_accuracy_and_recall(result):
    floors = BASELINE["matching"][result["name"]]
    assert result["status_accuracy"] >= floors["status_accuracy"] - 0.02
    assert result["findings_recall"] >= floors["findings_recall"] - 0.02


@pytest.mark.parametrize("result", ATTACKS, ids=[r["model"] for r in ATTACKS])
def test_every_recorded_attack_held_or_is_a_listed_limitation(result):
    known = set(BASELINE.get("attacks_known_failures", {}).get(result["model"], []))
    failed = {c["id"] for c in result["cases"] if c.get("result", "held" if c["passed"] else "failed") == "failed"}
    assert failed <= known, f"attacks that failed and are not listed as limitations: {sorted(failed - known)}"


def test_question_sets_do_not_overlap_and_every_case_has_acceptable_outcomes():
    dev = json.loads((EVAL / "questions_dev.json").read_text())
    test = json.loads((EVAL / "questions_test.json").read_text())
    assert (len(dev), len(test)) == (21, 9)
    assert not {q["exception_id"] for q in dev} & {q["exception_id"] for q in test}
    assert all(q["acceptable"] and q["kind"] in q["codes"] for q in dev + test)
