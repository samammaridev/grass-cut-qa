"""3x3 confusion matrix over the assignment flags + per-tag accuracy.

The fatal cell is truth in {good_to_pay, gcfu} -> flagged potential_fraud: it
must be 0. Disposition (auto vs human_review) is tracked separately — routing
an order to a human never erases its flag."""

from __future__ import annotations

from collections import Counter, defaultdict

OUTCOMES = ("good_to_pay", "gcfu", "potential_fraud")


def confusion(records: list[dict]) -> dict:
    cells: Counter = Counter()
    for r in records:
        cells[(r["truth"], r["predicted"])] += 1
    return dict(cells)


def fatal_cell_count(records: list[dict]) -> int:
    return sum(
        1 for r in records
        if r["truth"] in ("good_to_pay", "gcfu") and r["predicted"] == "potential_fraud"
    )


def agreement_rate(records: list[dict]) -> float:
    if not records:
        return 0.0
    return sum(1 for r in records if r["truth"] == r["predicted"]) / len(records)


def escalation_rate(records: list[dict]) -> float:
    """Share of orders routed to a human (disposition), regardless of flag."""
    if not records:
        return 0.0
    return sum(1 for r in records if r.get("disposition") == "human_review") / len(records)


def per_tag_accuracy(records: list[dict]) -> dict[str, float]:
    by_tag: dict[str, list[bool]] = defaultdict(list)
    for r in records:
        for tag in r.get("tags", []):
            by_tag[tag].append(r["truth"] == r["predicted"])
    return {tag: sum(hits) / len(hits) for tag, hits in sorted(by_tag.items())}


def render_matrix(records: list[dict]) -> str:
    cells = confusion(records)
    lines = ["truth \\ predicted".ljust(20) + "".join(o[:12].ljust(14) for o in OUTCOMES)]
    for truth in OUTCOMES:
        row = truth.ljust(20)
        for pred in OUTCOMES:
            row += str(cells.get((truth, pred), 0)).ljust(14)
        lines.append(row)
    lines.append("")
    lines.append(f"flag agreement: {agreement_rate(records):.1%}   "
                 f"human-review rate: {escalation_rate(records):.1%}   "
                 f"FATAL CELL (honest->fraud flag): {fatal_cell_count(records)}")
    tags = per_tag_accuracy(records)
    if tags:
        lines.append("per-tag accuracy: " + ", ".join(f"{t}={a:.0%}" for t, a in tags.items()))
    return "\n".join(lines)
