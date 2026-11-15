"""The evaluation's grader, checked on hand-made answers before any held-out run relies on it."""

from eval.run_eval import grade

PRICE = {
    "id": "p",
    "kind": "price_over_tolerance",
    "acceptable": ["request_credit_note", "hold"],
    "cite": {"value": 5.0, "what": "the 5.00% variance"},
}
NO_PO = {"id": "n", "kind": "no_po", "acceptable": ["hold", "request_corrected_invoice"], "cite": None}


def report(summary: str) -> dict:
    return {"summary": summary, "findings": []}


def test_an_acceptable_outcome_that_states_the_figure_passes():
    g = grade(
        PRICE,
        {"outcome": "request_credit_note", "rationale": ""},
        report("Billed 5.00% above the PO price of 20.00."),
        "completed",
    )
    assert g["passed"] and g["cited"] and g["outcome_ok"]


def test_the_figure_may_be_written_to_another_precision():
    assert grade(PRICE, {"outcome": "hold", "rationale": "Price is 5% over."}, report(""), "completed")["passed"]


def test_an_outcome_outside_the_acceptable_set_fails_even_with_the_figure():
    g = grade(PRICE, {"outcome": "approve_with_variance", "rationale": ""}, report("5.00% over"), "completed")
    assert not g["passed"] and "approve_with_variance" in g["reason"]


def test_a_right_outcome_without_the_deciding_figure_fails():
    g = grade(
        PRICE,
        {"outcome": "hold", "rationale": "The price is higher."},
        report("The price is higher than agreed."),
        "completed",
    )
    assert not g["passed"] and g["cited"] is False and "5.00%" in g["reason"]


def test_a_case_without_a_deciding_figure_is_graded_on_the_outcome_alone():
    assert grade(NO_PO, {"outcome": "hold", "rationale": ""}, report("No PO."), "completed")["passed"]


def test_no_recommendation_or_an_unfinished_run_fails():
    assert grade(NO_PO, None, report("No PO."), "completed")["reason"] == "no recommendation stored"
    assert not grade(NO_PO, None, None, "budget_exceeded")["passed"]


def test_a_different_number_does_not_count_as_the_figure():
    assert (
        grade(PRICE, {"outcome": "hold", "rationale": ""}, report("Billed 7.25% above the PO."), "completed")["cited"]
        is False
    )


QTY = {
    "id": "q",
    "kind": "quantity_over_received",
    "acceptable": ["hold"],
    "cite": {"value": 6.0, "what": "the 6 received"},
}


def test_a_small_whole_quantity_counts_but_not_inside_a_code():
    """Found in the first development run: 'shows only 6 received' was graded as not stating the 6."""
    assert grade(
        QTY, {"outcome": "hold", "rationale": ""}, report("GRN-E0006 shows only 6 received; 10 ordered."), "completed"
    )["cited"]
    assert (
        grade(
            QTY, {"outcome": "hold", "rationale": ""}, report("GRN-E0006 and PO-E0006 differ; 10 ordered."), "completed"
        )["cited"]
        is False
    )


def test_a_stray_small_number_does_not_count_as_a_percentage():
    g = grade(
        {**PRICE, "cite": {"value": 3.0, "what": "the 3.00% variance"}},
        {"outcome": "hold", "rationale": ""},
        report("Line 3 is over the price."),
        "completed",
    )
    assert g["cited"] is False
