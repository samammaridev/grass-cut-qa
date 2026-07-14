"""Cost gate: `python -m gcqa.costcheck` — does measured spend fit the predictions?

Reads runs/results.jsonl (every final payload carries real cost_usd and
per-stage token usage), prints the cost anatomy, projects $/month, and exits
non-zero when the mean breaches config cost_targets. Run it after every batch;
wire it into CI beside the quality regression gate — cost and quality are
gated together, never traded silently.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys

from .config import load_config


def analyze(records: list[dict]) -> dict:
    costed = [r for r in records if r.get("cost_usd") is not None]
    costs = [float(r["cost_usd"]) for r in costed]
    verifier_runs = sum(1 for r in costed if r.get("verifier"))
    denies = sum(
        1 for r in costed
        for g in (r.get("gate_events") or []) if g.get("action") in ("deny", "end_session")
    )
    out_tokens = []
    for r in costed:
        for stage in ("drafter", "verifier"):
            usage = ((r.get(stage) or {}).get("usage")) or {}
            if usage.get("output_tokens"):
                out_tokens.append(usage["output_tokens"])
    return {
        "orders": len(records),
        "costed": len(costed),
        "mean_usd": round(statistics.mean(costs), 4) if costs else None,
        "p95_usd": round(sorted(costs)[max(0, int(len(costs) * 0.95) - 1)], 4) if costs else None,
        "max_usd": round(max(costs), 4) if costs else None,
        "verifier_rate": round(verifier_runs / len(costed), 3) if costed else None,
        "gate_denies_per_order": round(denies / len(costed), 2) if costed else None,
        "mean_output_tokens_per_stage": int(statistics.mean(out_tokens)) if out_tokens else None,
        "human_review_rate": round(
            sum(1 for r in records
                if r.get("disposition") == "human_review"
                or r.get("outcome") == "escalate_to_human") / len(records), 3
        ) if records else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="gcqa cost gate")
    ap.add_argument("--config", default="config/qa.yaml")
    ap.add_argument("--results", default="runs/results.jsonl")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    path = cfg.root / args.results
    if not path.exists():
        print(f"no results at {path}", file=sys.stderr)
        return 2
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    stats = analyze(records)

    targets = cfg.cost_targets
    print(f"orders: {stats['orders']} (costed {stats['costed']})")
    print(f"cost/order: mean ${stats['mean_usd']}  p95 ${stats['p95_usd']}  max ${stats['max_usd']}")
    print(f"verifier rate: {stats['verifier_rate']}   gate denies/order: {stats['gate_denies_per_order']}")
    print(f"mean output tokens/stage: {stats['mean_output_tokens_per_stage']}   "
          f"human-review rate: {stats['human_review_rate']}")
    if stats["mean_usd"] is not None:
        monthly = stats["mean_usd"] * targets.monthly_orders
        print(f"projected @ {targets.monthly_orders} orders/mo: ${monthly:,.0f}")
        if stats["mean_usd"] > targets.per_order_usd_fail:
            print(f"FAIL: mean ${stats['mean_usd']} > fail target ${targets.per_order_usd_fail}")
            return 1
        if stats["mean_usd"] > targets.per_order_usd_warn:
            print(f"WARN: mean ${stats['mean_usd']} > warn target ${targets.per_order_usd_warn} "
                  "— check verifier rate, denies, and output tokens above")
            return 0
        print(f"PASS: within targets (warn ${targets.per_order_usd_warn} / "
              f"fail ${targets.per_order_usd_fail})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
