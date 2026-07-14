"""python -m gcqa.eval --config config/qa.yaml [--golden golden/golden.jsonl]
[--baseline golden/baseline.json] [--update-baseline] [--sweep]"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from ..config import load_config
from .matrix import render_matrix
from .regress import compare, load_baseline, save_baseline
from .runner import run_golden
from .sweep import render_sweep, sweep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="gcqa golden-set evaluation")
    ap.add_argument("--config", default="config/qa.yaml")
    ap.add_argument("--golden", default="golden/golden.jsonl")
    ap.add_argument("--baseline", default="golden/baseline.json")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--sweep", action="store_true", help="print the threshold sweep table")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        dotenv = cfg.root / ".env"
        if dotenv.exists():
            for line in dotenv.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()
                    break

    golden_path = cfg.root / args.golden
    if not golden_path.exists():
        print(f"no golden set at {golden_path} — create golden/golden.jsonl first", file=sys.stderr)
        return 2

    records = asyncio.run(run_golden(cfg, golden_path, cfg.root / "eval_runs"))
    print(render_matrix(records))

    if args.sweep:
        print("\nThreshold sweep (auto_decide for pay/gcfu; fraud fixed >= 0.95):")
        print(render_sweep(sweep(records)))

    baseline_path = cfg.root / args.baseline
    ok, messages = compare(records, load_baseline(baseline_path))
    for m in messages:
        print(m)
    if args.update_baseline:
        save_baseline(records, baseline_path)
        print(f"baseline updated: {baseline_path} (config {cfg.config_version})")
    print("regression gate:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
