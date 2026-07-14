"""Regression gate (CI): current eval records vs the committed baseline.

FAIL (exit 1): any fatal-cell case (truth honest -> predicted fraud);
              agreement drops > 2 pts vs baseline; any previously-passing case flips.
WARN (exit 0): escalation rate rises > 5 pts.
"""

from __future__ import annotations

import json
from pathlib import Path

from .matrix import agreement_rate, escalation_rate, fatal_cell_count

AGREEMENT_DROP_PTS = 0.02
ESCALATION_RISE_PTS = 0.05


def compare(records: list[dict], baseline: list[dict]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []

    fatal = fatal_cell_count(records)
    if fatal > 0:
        failures.append(f"FATAL CELL: {fatal} honest order(s) predicted potential_fraud — must be 0")

    if baseline:
        base_agree = agreement_rate(baseline)
        curr_agree = agreement_rate(records)
        if curr_agree < base_agree - AGREEMENT_DROP_PTS:
            failures.append(
                f"agreement dropped {base_agree:.1%} -> {curr_agree:.1%} (> {AGREEMENT_DROP_PTS:.0%} allowed)"
            )
        base_by_order = {r["order_number"]: r for r in baseline}
        for r in records:
            b = base_by_order.get(r["order_number"])
            if b and b["truth"] == b["predicted"] and r["truth"] != r["predicted"]:
                failures.append(
                    f"flip: {r['order_number']} was correct ({b['predicted']}), now {r['predicted']}"
                )
        base_esc = escalation_rate(baseline)
        curr_esc = escalation_rate(records)
        if curr_esc > base_esc + ESCALATION_RISE_PTS:
            warnings.append(
                f"WARN: escalation rate rose {base_esc:.1%} -> {curr_esc:.1%}"
            )

    return (len(failures) == 0), failures + warnings


def load_baseline(path: Path) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


def save_baseline(records: list[dict], path: Path) -> None:
    slim = [{k: r[k] for k in ("order_number", "truth", "predicted", "confidence", "tags")}
            for r in records]
    path.write_text(json.dumps(slim, indent=1))
