"""The Evaluation screen's data: every kind of recorded result, summarised, without the per-document rows."""

import json


async def test_recorded_results_of_each_kind_are_listed_in_summary(team, tmp_path, monkeypatch):
    from app.api import evaluation

    (tmp_path / "extraction-rules.json").write_text(json.dumps({"name": "extraction-rules", "mode": "rules", "field_accuracy": 0.9, "invented": 0,
                                                                "by_quality": {}, "by_field": {}, "rows": [{"big": "x"}]}))  # fmt: skip
    (tmp_path / "matching-scale0.1.json").write_text(
        json.dumps({"name": "matching-scale0.1", "status_accuracy": 0.97, "per_code": {}, "wrong_status": [{"d": 1}]})
    )
    (tmp_path / "test-m.json").write_text(
        json.dumps({"name": "test-m", "model": "m", "split": "test", "metrics": {"pass_rate": 0.8}, "questions": []})
    )
    (tmp_path / "attacks-m.json").write_text(json.dumps({"model": "m", "cases": [{"id": "loop", "result": "held"}]}))
    monkeypatch.setattr(evaluation, "RESULTS", tmp_path)
    body = (await team.viewer.get("/api/eval/results")).json()
    assert body["extraction"][0]["field_accuracy"] == 0.9 and "rows" not in body["extraction"][0]
    assert body["matching"][0]["status_accuracy"] == 0.97 and "wrong_status" not in body["matching"][0]
    assert body["results"][0]["metrics"]["pass_rate"] == 0.8 and "questions" not in body["results"][0]
    assert body["attacks"][0]["cases"][0]["result"] == "held"
