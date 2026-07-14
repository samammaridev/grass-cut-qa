"""C5 Adjudicator — flag + disposition (ORCHESTRATION.md §11).

The assignment defines exactly THREE flags: good_to_pay, gcfu, potential_fraud.
The agent always commits to one. Adjudication decides the DISPOSITION — who acts:

- "auto":         code acts on the flag (release payment / recommend follow-up)
- "human_review": a person confirms the flag first — low confidence, reviewer-
                  requested confirmation, drafter/verifier disagreement, failed
                  mandated verification, or a potential_fraud flag (Quality
                  Review always gets a human, per the assignment)

The flag is never erased by routing: an uncertain GCFU stays "GCFU — human
review", not a flagless escalation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import Config
from .prechecks import Signals
from .review import ReviewResult, review_result_summary

AUTO = "auto"
HUMAN = "human_review"


def needs_verifier(draft: ReviewResult, signals: Signals, cfg: Config) -> bool:
    if draft.verdict is None:
        return False                            # nothing to verify — human-routed anyway
    flag = draft.verdict["outcome"]
    triggers = set(cfg.gates.verifier_runs_on)
    if flag in triggers:
        return True
    if "low_confidence" in triggers and (
        float(draft.verdict["confidence"]) < cfg.thresholds.verifier_below_confidence
    ):
        return True
    if "location_unverified" in triggers and (
        signals.gps.get("location_match") == "unverified"
    ):
        return True
    return False


def decide(
    draft_verdict: dict | None,
    verifier_verdict: dict | None,
    short_circuit_reason: str | None,
    auto_decide: dict[str, float],
    fraud_requires_verifier_agreement: bool,
    draft_error: str | None = None,
) -> tuple[str | None, str, float | None, list[str]]:
    """The pure decision function — shared by C5 and the eval threshold sweep.

    Returns (flag, disposition, confidence, adjudication_notes).
    """
    notes: list[str] = []

    if short_circuit_reason is not None:
        # Documentation/validity problems: per the assignment, inadequate
        # documentation is GCFU — a human confirms before a follow-up issues.
        return "gcfu", HUMAN, None, [f"pre-check short-circuit: {short_circuit_reason}"]
    if draft_verdict is None:
        return None, HUMAN, None, [f"no accepted draft verdict ({draft_error}) — unclassified"]

    flag = draft_verdict["outcome"]
    confidence = float(draft_verdict["confidence"])
    verifier_ran = verifier_verdict is not None
    disposition = AUTO

    if draft_verdict.get("needs_human"):
        disposition = HUMAN
        notes.append("reviewer requested human confirmation")
    threshold = auto_decide.get(flag)
    if threshold is not None and confidence < threshold:
        disposition = HUMAN
        notes.append(f"confidence {confidence:.2f} below auto_decide[{flag}]={threshold}")
    if verifier_ran and verifier_verdict["outcome"] != flag:
        disposition = HUMAN
        notes.append(
            f"verifier flagged {verifier_verdict['outcome']!r} vs drafter {flag!r} — human decides"
        )
    if verifier_ran and verifier_verdict.get("needs_human") and disposition == AUTO:
        disposition = HUMAN
        notes.append("verifier requested human confirmation")

    if flag == "potential_fraud":
        disposition = HUMAN                     # Quality Review is always human
        if fraud_requires_verifier_agreement and not (
            verifier_ran and verifier_verdict["outcome"] == "potential_fraud"
        ):
            notes.append(
                "fraud flag UNCONFIRMED — "
                + ("independent verifier disagreed" if verifier_ran else "verifier unavailable")
            )
        else:
            notes.append("fraud flag confirmed by independent verifier — Quality Review")

    return flag, disposition, confidence, notes


def adjudicate(
    order_number: str,
    draft: ReviewResult,
    verifier: ReviewResult | None,
    signals: Signals,
    cfg: Config,
    run_id: str = "",
) -> dict:
    verifier_ran = verifier is not None and verifier.verdict is not None
    flag, disposition, confidence, notes = decide(
        draft.verdict,
        verifier.verdict if verifier_ran else None,
        signals.short_circuit_reason if signals.short_circuit else None,
        cfg.thresholds.auto_decide,
        cfg.gates.fraud_requires_verifier_agreement,
        draft_error=draft.error,
    )
    # A MANDATED verifier that was attempted but produced no accepted verdict
    # must not silently degrade to "verifier never ran".
    if verifier is not None and verifier.verdict is None and disposition == AUTO:
        disposition = HUMAN
        notes.append(f"mandated verifier failed to produce a verdict ({verifier.error})")

    verifier_flag = verifier.verdict["outcome"] if verifier_ran else None
    agreement = (verifier_flag == flag) if (verifier_ran and flag) else None
    fraud_confirmed = (
        flag == "potential_fraud" and verifier_ran and verifier_flag == "potential_fraud"
    ) if flag == "potential_fraud" else None

    indicators = []
    for sig in signals.fraud_signals:
        indicators.append(f"deterministic: {sig}")
    for src_result in (draft, verifier):
        if src_result is not None and src_result.verdict:
            for item in (src_result.verdict.get("fraud_indicators") or []):
                if item and item not in indicators:
                    indicators.append(item)

    cost = draft.cost_usd + (verifier.cost_usd if verifier else 0.0)
    return {
        "order_number": order_number,
        "review_stage": "final",
        "flag": flag,                            # the assignment classification — always present
        "anomaly": bool(indicators),             # fraud/integrity anomalies on record — independent
                                                 # of the flag: an order can fit the standard
                                                 # (good_to_pay) and still carry anomalies
        "fraud_indicators": indicators,
        "disposition": disposition,              # auto | human_review
        "outcome": flag,                         # legacy alias for older tooling
        "confidence": confidence,
        "verifier_flag": verifier_flag,
        "fraud_confirmed": fraud_confirmed,
        "adjudication": notes,
        "drafter": review_result_summary(draft),
        "verifier": review_result_summary(verifier) if verifier else None,
        "agreement": agreement,
        "thresholds_applied": {
            "auto_decide": dict(cfg.thresholds.auto_decide),
            "verifier_below_confidence": cfg.thresholds.verifier_below_confidence,
        },
        "gate_events": draft.gate_events + (verifier.gate_events if verifier else []),
        "signals": signals.to_dict(),
        "models": {
            "drafter": cfg.models["drafter"] if draft.verdict else None,
            "verifier": cfg.models["verifier"] if verifier_ran else None,
        },
        "cost_usd": round(cost, 4),
        "session_ids": [s for s in (draft.session_id,
                                    verifier.session_id if verifier else None) if s],
        "config_version": cfg.config_version,
        "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run_id,
    }
