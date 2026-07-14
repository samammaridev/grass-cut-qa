"""Report renderer tests: both variants render, facts come from the payload,
and a render failure never blocks the verdict."""

from gcqa.report import render_report, write_report


def final_payload(outcome="good_to_pay", **overrides) -> dict:
    p = {
        "order_number": "500119014", "review_stage": "final",
        "flag": outcome, "disposition": "auto",
        "verifier_flag": None, "fraud_confirmed": None,
        "outcome": outcome, "confidence": 0.87,
        "adjudication": [],
        "drafter": {
            "role": "drafter", "outcome": "good_to_pay", "confidence": 0.87,
            "evidence": [
                {"claim": "rear yard cut", "source_type": "photo",
                 "photo_guid": "449251ac-6533", "field_key": None,
                 "observation": "After: rear lawn uniformly cut"},
            ],
            "checks": {"location_match": "pass", "time_on_site": "pass",
                       "four_sides": "pass", "before_after_diff": "pass",
                       "extraordinary_conditions": False},
            "flag_reasons": ["all four sides mowed to standard",
                             "location confirmed by GPS and house number"],
            "review_items": ["Confirm stray photo 57cad809 belongs to another order"],
            "recommended_actions": ["release_payment"],
            "rationale_plain": "The After photos show the lawn cut (449251ac).",
            "notes_for_human": None, "subtype": "success", "cost_usd": 0.06,
            "session_id": "ses_1", "gate_events": [], "error": None,
        },
        "verifier": None, "agreement": None,
        "thresholds_applied": {"auto_decide": {"good_to_pay": 0.85}},
        "gate_events": [], "signals": {
            "in_season": True, "photo_count": 32, "photo_count_ok": True,
            "time_on_site_min": 38, "time_on_site_ok": True,
            "photo_time_span_min": 171, "date_skew_days": 0,
            "documentation_gaps": [], "fraud_signals": [],
            "gps": {"medoid": [39.903871, -83.813378], "valid": 42, "unknown": 0,
                    "outliers": [{"guid": "57cad809-x", "dist_m": 723291,
                                  "descPrefix": "Condition", "descText": "Rear of House"}],
                    "mixed_clusters": False, "location_match": "pass",
                    "medoid_to_property_m": 3,
                    "property_geocode": {"source": "census", "quality": "exact"}},
        },
        "models": {"drafter": "claude-sonnet-5", "verifier": None},
        "cost_usd": 0.065, "session_ids": ["ses_1"],
        "config_version": "sha256:ab12", "decided_at": "2026-07-13T12:00:00+00:00",
        "run_id": "test", "latency_s": 96,
    }
    p.update(overrides)
    return p


FIELDS = {"address": "1801 WOODWARD AVE", "city": "SPRINGFIELD", "state": "OH",
          "zip": "45506", "work_code_desc": "MONTH/BIMONTHLY GRASS CUT", "vendor": "DEVGUY"}


def test_good_to_pay_report():
    text = render_report(final_payload(), FIELDS, "runs/2026-07/500119014.json")
    assert "FLAG: GOOD TO PAY" in text
    assert "Decision — why GOOD TO PAY" in text
    assert "all four sides mowed to standard" in text
    assert "- [ ] Confirm stray photo 57cad809" in text
    assert "confidence 0.87" in text
    assert "1801 WOODWARD AVE" in text
    assert "57cad809 @ 723.3 km" in text
    assert "3 m from geocoded property (census/exact)" in text
    assert "449251ac" in text
    assert "not required" in text                      # verifier line
    assert "sha256:ab12" in text


def test_human_review_report_leads_with_flag():
    final = final_payload(outcome="gcfu")
    final["flag"] = "gcfu"
    final["disposition"] = "human_review"
    final["adjudication"] = ["confidence 0.62 below auto_decide[gcfu]=0.75"]
    text = render_report(final, FIELDS, "runs/x.json")
    assert text.startswith("# 🚩 FLAG: GCFU — needs human review")
    assert "below auto_decide" in text
    assert "**Check first:** photos 449251ac" in text


def test_unconfirmed_fraud_flag_is_explicit():
    final = final_payload(outcome="potential_fraud")
    final["flag"] = "potential_fraud"
    final["disposition"] = "human_review"
    final["fraud_confirmed"] = False
    final["verifier_flag"] = "gcfu"
    final["adjudication"] = ["fraud flag UNCONFIRMED — independent verifier disagreed"]
    text = render_report(final, FIELDS, "runs/x.json")
    assert "FLAG: POTENTIAL FRAUD" in text
    assert "UNCONFIRMED" in text
    assert "verifier flagged gcfu" in text


def test_flag_survives_demotion_not_superseded():
    # demotion changes disposition, not the flag -> narrative is NOT superseded
    final = final_payload(outcome="good_to_pay")
    final["disposition"] = "human_review"
    final["adjudication"] = ["confidence 0.70 below auto_decide[good_to_pay]=0.75"]
    text = render_report(final, FIELDS, "runs/x.json")
    assert "superseded" not in text
    assert "FLAG: GOOD TO PAY" in text


def test_render_failure_writes_stub(tmp_path):
    broken = final_payload()
    del broken["signals"]                              # break the template inputs
    path = write_report(broken, FIELDS, tmp_path / "r.md", "runs/x.json")
    text = path.read_text()
    assert "Report rendering failed" in text
    assert "good_to_pay" in text                       # verdict still visible


def test_fraud_indicators_render_as_warnings_not_flag():
    """Indicators section renders prominently on a NON-fraud order and says
    explicitly it is not the flag (the user's fraud-indicator model)."""
    final = final_payload(outcome="gcfu")
    final["flag"] = "gcfu"
    final["disposition"] = "human_review"
    final["adjudication"] = ["reviewer requested human confirmation"]
    final["fraud_indicators"] = [
        "deterministic: staged_documentation",
        "staged before/after timestamps on 4 stations",
    ]
    text = render_report(final, FIELDS, "runs/x.json")
    assert "Potential-fraud indicators (2)" in text
    assert "warnings only, not the flag" in text
    assert "staged before/after timestamps on 4 stations" in text
    assert "FLAG: GCFU" in text                        # order-level flag unchanged


def test_fraud_indicators_absent_section_on_clean_order():
    final = final_payload()
    final["fraud_indicators"] = []
    text = render_report(final, FIELDS, "runs/x.json")
    assert "Potential-fraud indicators" not in text


def test_fraud_flag_report_itemizes_indicators_without_warning_phrase():
    final = final_payload(outcome="potential_fraud")
    final["flag"] = "potential_fraud"
    final["disposition"] = "human_review"
    final["fraud_confirmed"] = True
    final["fraud_indicators"] = ["deterministic: wrong_property"]
    text = render_report(final, FIELDS, "runs/x.json")
    assert "Potential-fraud indicators (1)" in text
    assert "warnings only" not in text
