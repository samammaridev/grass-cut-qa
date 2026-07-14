"""Threshold sweep — pure post-processing over stored eval records.

Confidences and both verdicts are stored per record, so re-adjudicating at a
different auto_decide point needs zero re-inference (ORCHESTRATION.md §12).
Ops reads this table and picks the operating point in config/qa.yaml.
"""

from __future__ import annotations

from ..adjudicate import decide
from .matrix import agreement_rate, escalation_rate, fatal_cell_count


def readjudicate(record: dict, auto_decide: dict[str, float],
                 fraud_requires_verifier_agreement: bool = True) -> tuple[str | None, str]:
    """Returns (flag, disposition) for the record at the given thresholds."""
    final = record.get("final") or {}
    drafter = final.get("drafter") or {}
    verifier = final.get("verifier") or {}
    draft_verdict = (
        {"outcome": drafter["outcome"], "confidence": drafter["confidence"],
         "needs_human": drafter.get("needs_human")}
        if drafter.get("outcome") else None
    )
    verifier_verdict = (
        {"outcome": verifier["outcome"], "confidence": verifier["confidence"],
         "needs_human": verifier.get("needs_human")}
        if verifier and verifier.get("outcome") else None
    )
    short_circuit = None
    for note in final.get("adjudication", []):
        if note.startswith(("short_circuit", "pre-check short-circuit")):
            short_circuit = note
    flag, disposition, _, _ = decide(draft_verdict, verifier_verdict, short_circuit,
                                     auto_decide, fraud_requires_verifier_agreement)
    return flag, disposition


def sweep(records: list[dict], lo: float = 0.50, hi: float = 0.95,
          step: float = 0.05) -> list[dict]:
    """Sweep a single auto_decide level applied to all outcomes except fraud
    (fraud keeps its own stricter level fixed at the 0.95 default)."""
    rows = []
    t = lo
    while t <= hi + 1e-9:
        auto = {"good_to_pay": t, "gcfu": t, "potential_fraud": max(t, 0.95)}
        swept = []
        for r in records:
            if r.get("final"):
                flag, disposition = readjudicate(r, auto)
                swept.append({**r, "predicted": flag, "disposition": disposition})
            else:
                swept.append(r)
        rows.append({
            "threshold": round(t, 2),
            "agreement": round(agreement_rate(swept), 3),
            "human_review_rate": round(escalation_rate(swept), 3),
            "automation_rate": round(1 - escalation_rate(swept), 3),
            "fatal_cell": fatal_cell_count(swept),
        })
        t += step
    return rows


def render_sweep(rows: list[dict]) -> str:
    lines = ["threshold  agreement  automation  human-rev  FATAL"]
    for r in rows:
        lines.append(f"{r['threshold']:<10} {r['agreement']:<10.1%} "
                     f"{r['automation_rate']:<11.1%} {r['human_review_rate']:<10.1%} "
                     f"{r['fatal_cell']}")
    return "\n".join(lines)
