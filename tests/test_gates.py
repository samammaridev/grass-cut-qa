"""C3 gate tests: every deny class, the per-class deny policy, and the
transport-parity corpus (same pure function serves hook and batch validator)."""

import json
from pathlib import Path


from gcqa.gates import (
    GateContext,
    apply_deny_policy,
    evaluate_submit,
    to_hook_deny,
)
from gcqa.prechecks import Signals

GUIDS = {"449251ac-6533-419c-be00-695a0893c3bb", "57cad809-aaaa-bbbb-cccc-000000000000"}


def make_ctx(config, fraud_signals=None) -> GateContext:
    return GateContext(
        downloaded_guids=set(GUIDS),
        signals=Signals(fraud_signals=fraud_signals or []),
        cfg=config,
    )


def valid_payload(**overrides) -> dict:
    p = {
        "order_number": "500119014",
        "outcome": "good_to_pay",
        "confidence": 0.9,
        "needs_human": False,
        "evidence": [
            {"claim": "rear cut", "source_type": "photo",
             "photo_guid": "449251ac-6533-419c-be00-695a0893c3bb",
             "observation": "After: rear lawn cut"},
            {"claim": "front cut", "source_type": "photo", "photo_guid": "449251ac",
             "observation": "After: front lawn cut"},
        ],
        "checks": {"location_match": "pass", "time_on_site": "pass", "four_sides": "pass",
                   "before_after_diff": "pass", "extraordinary_conditions": False},
        "flag_reasons": ["lawn cut to standard across all sides"],
        "review_items": [],
        "recommended_actions": ["release_payment"],
        "rationale_plain": "The photos show the lawn cut to standard (449251ac).",
        "notes_for_human": None,
    }
    p.update(overrides)
    return p


def test_valid_payload_passes(config):
    assert evaluate_submit(valid_payload(), make_ctx(config)) is None


def test_bad_outcome(config):
    deny = evaluate_submit(valid_payload(outcome="pay_now"), make_ctx(config))
    assert deny.clazz == "schema" and deny.kind == "substantive"


def test_confidence_bounds(config):
    assert evaluate_submit(valid_payload(confidence=1.5), make_ctx(config)).clazz == "confidence_bounds"
    assert evaluate_submit(valid_payload(confidence="high"), make_ctx(config)).clazz == "confidence_bounds"


def test_empty_evidence_denied_for_all_flags(config):
    assert evaluate_submit(valid_payload(evidence=[]), make_ctx(config)).clazz == "empty_evidence"
    # a documentation gap cited as signal/field evidence satisfies gcfu
    ok = valid_payload(outcome="gcfu", needs_human=True,
                       evidence=[{"claim": "documentation incomplete", "source_type": "signal",
                                  "field_key": "documentation_gaps",
                                  "observation": "no usable after photos for the rear"}],
                       rationale_plain="Documentation is incomplete for the rear areas.")
    assert evaluate_submit(ok, make_ctx(config)) is None


def test_escalate_is_no_longer_a_flag(config):
    deny = evaluate_submit(valid_payload(outcome="escalate_to_human"), make_ctx(config))
    assert deny.clazz == "schema" and "needs_human" in deny.reason


def test_unknown_guid_denied(config):
    bad = valid_payload()
    bad["evidence"][0]["photo_guid"] = "deadbeef-0000-0000-0000-000000000000"
    assert evaluate_submit(bad, make_ctx(config)).clazz == "evidence_integrity"


def test_fraud_photo_floor(config):
    fraud = valid_payload(outcome="potential_fraud", confidence=0.96)
    fraud["evidence"] = [fraud["evidence"][0]]        # only 1 photo-cited item
    fraud["rationale_plain"] = "Photos show no work at the property (449251ac)."
    deny = evaluate_submit(fraud, make_ctx(config, fraud_signals=["wrong_property"]))
    assert deny.clazz == "fraud_evidence_floor"
    assert "needs_human" in deny.reason               # actionable feedback


def test_fraud_needs_deterministic_corroboration(config):
    fraud = valid_payload(outcome="potential_fraud", confidence=0.96,
                          rationale_plain="Photos show no work done (449251ac).")
    deny = evaluate_submit(fraud, make_ctx(config, fraud_signals=[]))
    assert deny.clazz == "fraud_corroboration"
    ok = evaluate_submit(fraud, make_ctx(config, fraud_signals=["wrong_property"]))
    assert ok is None


def test_gcfu_must_name_area(config):
    gcfu = valid_payload(outcome="gcfu",
                         rationale_plain="Some areas were not brought to standard (449251ac).")
    for e in gcfu["evidence"]:
        e["observation"] = "  "
    assert evaluate_submit(gcfu, make_ctx(config)).clazz == "gcfu_area"


# --- lint rules ----------------------------------------------------------------

def test_lint_length(config):
    long = valid_payload(rationale_plain="word " * 130)
    assert evaluate_submit(long, make_ctx(config)).clazz == "lint_length"


def test_lint_unknown_guid_in_narrative(config):
    bad = valid_payload(rationale_plain="Photo deadbeef shows the lawn cut.")
    assert evaluate_submit(bad, make_ctx(config)).clazz == "lint_unknown_guid"


def test_lint_outcome_word_contradiction(config):
    bad = valid_payload(rationale_plain="This looks like fraud to me (449251ac).")
    assert evaluate_submit(bad, make_ctx(config)).clazz == "lint_outcome_word"
    ok = valid_payload(outcome="potential_fraud", confidence=0.96,
                       rationale_plain="Photos indicate fraudulent documentation (449251ac).")
    assert evaluate_submit(ok, make_ctx(config, fraud_signals=["wrong_property"])) is None


def test_lint_unsourced_numerals(config):
    bad = valid_payload(rationale_plain="The vendor spent 99 minutes on site (449251ac).")
    assert evaluate_submit(bad, make_ctx(config)).clazz == "lint_unsourced_number"
    # word-numbers are fine
    ok = valid_payload(rationale_plain="All four sides were addressed (449251ac).")
    assert evaluate_submit(ok, make_ctx(config)) is None


# --- deny policy: per-class semantics --------------------------------------------

def test_two_substantive_denies_end_session(config):
    ctx = make_ctx(config)
    deny = evaluate_submit(valid_payload(evidence=[]), ctx)
    assert apply_deny_policy(ctx, deny).action == "deny"
    deny2 = evaluate_submit(valid_payload(evidence=[]), ctx)
    assert apply_deny_policy(ctx, deny2).action == "end_session"


def test_two_lint_denies_accept_with_stub(config):
    ctx = make_ctx(config)
    deny = evaluate_submit(valid_payload(rationale_plain="word " * 130), ctx)
    assert apply_deny_policy(ctx, deny).action == "deny"
    deny2 = evaluate_submit(valid_payload(rationale_plain="Photo deadbeef shows it."), ctx)
    action = apply_deny_policy(ctx, deny2)
    assert action.action == "accept_with_stub"        # lint never blocks a valid verdict


def test_hook_deny_shape(config):
    deny = evaluate_submit(valid_payload(evidence=[]), make_ctx(config))
    out = to_hook_deny(deny)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecisionReason"]


# --- transport parity corpus ------------------------------------------------------

def test_gate_parity_corpus(config):
    """The same pure function serves the SDK hook and the batch validator; this
    corpus pins its decisions so neither transport can drift."""
    corpus = [
        (valid_payload(), None),
        (valid_payload(outcome="nope"), "schema"),
        (valid_payload(confidence=2), "confidence_bounds"),
        (valid_payload(evidence=[]), "empty_evidence"),
        (valid_payload(rationale_plain="word " * 200), "lint_length"),
    ]
    corpus_path = Path(__file__).parent / "fixtures" / "verdict_corpus.json"
    corpus_path.write_text(json.dumps([p for p, _ in corpus], indent=1))
    replayed = json.loads(corpus_path.read_text())
    for payload, expected in zip(replayed, [e for _, e in corpus]):
        deny = evaluate_submit(payload, make_ctx(config))
        assert (deny.clazz if deny else None) == expected


def test_missing_flag_reasons_denied(config):
    deny = evaluate_submit(valid_payload(flag_reasons=[]), make_ctx(config))
    assert deny.clazz == "flag_reasons" and deny.kind == "substantive"
