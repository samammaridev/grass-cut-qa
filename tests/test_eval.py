"""Eval harness logic tests (offline — matrix, sweep, regression gate)."""

from gcqa.eval.matrix import (agreement_rate, confusion, escalation_rate,
                              fatal_cell_count, per_tag_accuracy)
from gcqa.eval.regress import compare
from gcqa.eval.sweep import readjudicate, sweep


def rec(truth, predicted, confidence=0.9, tags=(), drafter_outcome=None,
        verifier_outcome=None, disposition="auto"):
    final = {
        "adjudication": [],
        "drafter": {"outcome": drafter_outcome or predicted, "confidence": confidence,
                    "needs_human": False},
        "verifier": ({"outcome": verifier_outcome, "confidence": 0.9,
                      "needs_human": False}
                     if verifier_outcome else None),
    }
    return {"order_number": f"{truth}-{predicted}-{confidence}", "truth": truth,
            "predicted": predicted, "disposition": disposition,
            "confidence": confidence, "tags": list(tags), "final": final}


def test_matrix_and_rates():
    records = [
        rec("good_to_pay", "good_to_pay"),
        rec("good_to_pay", "good_to_pay", disposition="human_review"),
        rec("gcfu", "gcfu"),
        rec("potential_fraud", "potential_fraud", disposition="human_review"),
        rec("good_to_pay", "potential_fraud", disposition="human_review"),  # fatal cell
    ]
    cells = confusion(records)
    assert cells[("good_to_pay", "good_to_pay")] == 2
    assert fatal_cell_count(records) == 1
    assert agreement_rate(records) == 0.8
    assert escalation_rate(records) == 0.6            # human-review share


def test_per_tag_accuracy():
    records = [rec("gcfu", "gcfu", tags=["borderline"]),
               rec("gcfu", "good_to_pay", tags=["borderline"])]
    assert per_tag_accuracy(records) == {"borderline": 0.5}


def test_readjudicate_flag_survives_threshold():
    r = rec("good_to_pay", "good_to_pay", confidence=0.76)
    flag, disposition = readjudicate(r, {"good_to_pay": 0.85, "gcfu": 0.85,
                                         "potential_fraud": 0.95})
    assert (flag, disposition) == ("good_to_pay", "human_review")
    flag, disposition = readjudicate(r, {"good_to_pay": 0.70, "gcfu": 0.70,
                                         "potential_fraud": 0.95})
    assert (flag, disposition) == ("good_to_pay", "auto")


def test_readjudicate_fraud_always_human():
    r = rec("potential_fraud", "potential_fraud", confidence=0.97,
            drafter_outcome="potential_fraud")             # no verifier stored
    assert readjudicate(r, {"potential_fraud": 0.95}) == ("potential_fraud", "human_review")
    r2 = rec("potential_fraud", "potential_fraud", confidence=0.97,
             drafter_outcome="potential_fraud", verifier_outcome="potential_fraud")
    assert readjudicate(r2, {"potential_fraud": 0.95}) == ("potential_fraud", "human_review")


def test_sweep_monotonic_automation():
    records = [rec("good_to_pay", "good_to_pay", confidence=c)
               for c in (0.6, 0.7, 0.8, 0.9)]
    rows = sweep(records, lo=0.55, hi=0.95, step=0.1)
    automation = [r["automation_rate"] for r in rows]
    assert automation == sorted(automation, reverse=True)  # stricter -> less automation
    assert all(r["fatal_cell"] == 0 for r in rows)


def test_regression_gate_fatal_cell_fails():
    ok, messages = compare([rec("good_to_pay", "potential_fraud")], [])
    assert not ok and "FATAL" in messages[0]


def test_regression_gate_flip_fails():
    baseline = [{"order_number": "A", "truth": "gcfu", "predicted": "gcfu",
                 "confidence": 0.9, "tags": []}]
    current = [{**rec("gcfu", "escalate_to_human"), "order_number": "A"}]
    ok, messages = compare(current, baseline)
    assert not ok and any("flip" in m for m in messages)


def test_regression_gate_human_review_rise_warns_only():
    baseline = [{"order_number": str(i), "truth": "good_to_pay",
                 "predicted": "good_to_pay", "confidence": 0.9, "tags": []}
                for i in range(10)]
    current = [{**rec("good_to_pay", "good_to_pay"), "order_number": str(i)} for i in range(9)]
    current.append({**rec("good_to_pay", "good_to_pay", disposition="human_review"),
                    "order_number": "9"})
    ok, messages = compare(current, baseline)
    assert ok and any("WARN" in m for m in messages)
