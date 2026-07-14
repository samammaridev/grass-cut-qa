"""C5 truth-table tests — flag + disposition (§11).

The flag (assignment classification) is never erased by routing: demotion
changes the DISPOSITION to human_review, the flag survives.
"""

from gcqa.adjudicate import adjudicate, needs_verifier
from gcqa.prechecks import Signals
from gcqa.review import ReviewResult


def rr(role="drafter", outcome="good_to_pay", confidence=0.9, needs_human=False,
       error=None) -> ReviewResult:
    verdict = None if outcome is None else {
        "order_number": "1", "outcome": outcome, "confidence": confidence,
        "needs_human": needs_human,
        "evidence": [{"claim": "x", "source_type": "photo", "photo_guid": "g",
                      "observation": "o"}],
        "checks": {}, "recommended_actions": [], "rationale_plain": "r",
    }
    return ReviewResult(role=role, verdict=verdict, cost_usd=0.05,
                        session_id=f"s-{role}", error=error)


def sig(**kw) -> Signals:
    s = Signals()
    s.gps = {"location_match": kw.pop("location_match", "pass")}
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_short_circuit_flags_gcfu_for_human(config):
    s = sig()
    s.short_circuit = "escalate_to_human"
    s.short_circuit_reason = "no photo documentation available"
    out = adjudicate("1", ReviewResult(role="drafter", error="short-circuited"), None, s, config)
    assert out["flag"] == "gcfu"                      # inadequate documentation = GCFU
    assert out["disposition"] == "human_review"
    assert "short-circuit" in out["adjudication"][0]


def test_no_draft_is_unclassified_human(config):
    out = adjudicate("1", rr(outcome=None, error="no submit"), None, sig(), config)
    assert out["flag"] is None and out["disposition"] == "human_review"


def test_confident_pass_auto(config):
    out = adjudicate("1", rr(confidence=0.87), None, sig(), config)
    assert out["flag"] == "good_to_pay" and out["disposition"] == "auto"


def test_low_confidence_keeps_flag_routes_human(config):
    out = adjudicate("1", rr(outcome="gcfu", confidence=0.62), None, sig(), config)
    assert out["flag"] == "gcfu"                      # the flag SURVIVES demotion
    assert out["disposition"] == "human_review"
    assert any("below auto_decide" in a for a in out["adjudication"])


def test_needs_human_routes_even_when_confident(config):
    out = adjudicate("1", rr(confidence=0.95, needs_human=True), None, sig(), config)
    assert out["flag"] == "good_to_pay"
    assert out["disposition"] == "human_review"
    assert any("requested human confirmation" in a for a in out["adjudication"])


def test_fraud_flag_always_human_with_confirmation_status(config):
    draft = rr(outcome="potential_fraud", confidence=0.96)
    # no verifier -> flag preserved, marked unconfirmed
    out = adjudicate("1", draft, None, sig(), config)
    assert out["flag"] == "potential_fraud"
    assert out["disposition"] == "human_review"
    assert out["fraud_confirmed"] is False
    # verifier disagrees -> still flagged fraud, unconfirmed, human sees both flags
    v_gcfu = rr(role="verifier", outcome="gcfu", confidence=0.9)
    out = adjudicate("1", draft, v_gcfu, sig(), config)
    assert out["flag"] == "potential_fraud" and out["verifier_flag"] == "gcfu"
    assert out["fraud_confirmed"] is False and out["disposition"] == "human_review"
    # verifier agrees -> confirmed fraud, still a human (Quality Review)
    v_fraud = rr(role="verifier", outcome="potential_fraud", confidence=0.97)
    out = adjudicate("1", draft, v_fraud, sig(), config)
    assert out["flag"] == "potential_fraud" and out["fraud_confirmed"] is True
    assert out["disposition"] == "human_review" and out["agreement"] is True


def test_disagreement_routes_human_keeps_both_flags(config):
    out = adjudicate("1", rr(outcome="gcfu", confidence=0.9),
                     rr(role="verifier", outcome="good_to_pay"), sig(), config)
    assert out["flag"] == "gcfu" and out["verifier_flag"] == "good_to_pay"
    assert out["disposition"] == "human_review" and out["agreement"] is False


def test_agreeing_gcfu_auto(config):
    out = adjudicate("1", rr(outcome="gcfu", confidence=0.9),
                     rr(role="verifier", outcome="gcfu"), sig(), config)
    assert out["flag"] == "gcfu" and out["disposition"] == "auto"


def test_failed_mandated_verifier_routes_human(config):
    draft = rr(outcome="gcfu", confidence=0.9)
    failed_verifier = rr(role="verifier", outcome=None, error="error_max_turns")
    out = adjudicate("1", draft, failed_verifier, sig(), config)
    assert out["flag"] == "gcfu"
    assert out["disposition"] == "human_review"
    assert any("mandated verifier failed" in a for a in out["adjudication"])


def test_final_payload_fields(config):
    out = adjudicate("1", rr(confidence=0.87), None, sig(), config, run_id="r1")
    assert out["config_version"].startswith("sha256:")
    assert out["models"]["drafter"] == "claude-sonnet-5"
    assert out["session_ids"] == ["s-drafter"]
    assert out["run_id"] == "r1"
    assert out["outcome"] == out["flag"]              # legacy alias


# --- verifier trigger ----------------------------------------------------------

def test_needs_verifier_matrix(config):
    assert needs_verifier(rr(outcome="potential_fraud", confidence=0.99), sig(), config)
    assert needs_verifier(rr(outcome="gcfu", confidence=0.99), sig(), config)
    assert not needs_verifier(rr(confidence=0.9), sig(), config)
    assert needs_verifier(rr(confidence=0.8), sig(), config)              # low confidence
    assert needs_verifier(rr(confidence=0.9), sig(location_match="unverified"), config)
    assert not needs_verifier(rr(outcome=None), sig(), config)


def test_fraud_indicators_aggregate_without_flag_inflation(config):
    """Fraud INDICATORS travel with a non-fraud flag (deterministic signals +
    both reviewers' observations, deduped) — the order-level flag stays
    work-based. Pinned per the dev-set finding: staging is systemic, so
    indicator-level visibility, not flag inflation, is the design."""
    draft = rr(outcome="gcfu", confidence=0.8)
    draft.verdict["fraud_indicators"] = [
        "staged before/after timestamps on 4 stations",
        "After photo shows uncut grass",
    ]
    verifier = rr(role="verifier", outcome="gcfu", confidence=0.8)
    verifier.verdict["fraud_indicators"] = [
        "After photo shows uncut grass",              # dup with drafter -> once
        "photo of a different house filed as Rear of House",
    ]
    s = sig(fraud_signals=["staged_documentation", "photo_reuse"])
    out = adjudicate("1", draft, verifier, s, config)
    assert out["flag"] == "gcfu"                      # NOT potential_fraud
    assert out["fraud_indicators"] == [
        "deterministic: staged_documentation",
        "deterministic: photo_reuse",
        "staged before/after timestamps on 4 stations",
        "After photo shows uncut grass",
        "photo of a different house filed as Rear of House",
    ]


def test_fraud_indicators_empty_on_clean_order(config):
    out = adjudicate("1", rr(outcome="good_to_pay", confidence=0.95), None, sig(), config)
    assert out["fraud_indicators"] == []


def test_anomaly_flag_rides_with_good_to_pay(config):
    """The user-specified triple: flag=good_to_pay + anomaly=true +
    disposition=human_review is a valid, first-class answer."""
    draft = rr(outcome="good_to_pay", confidence=0.9, needs_human=True)
    draft.verdict["fraud_indicators"] = ["payment evidence is time-staged"]
    out = adjudicate("1", draft, None, sig(fraud_signals=["staged_documentation"]), config)
    assert out["flag"] == "good_to_pay"
    assert out["anomaly"] is True
    assert out["disposition"] == "human_review"


def test_anomaly_false_on_clean_order(config):
    out = adjudicate("1", rr(outcome="good_to_pay", confidence=0.95), None, sig(), config)
    assert out["anomaly"] is False
