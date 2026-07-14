"""Intake CLI + per-order orchestration: C0 -> C1 -> (short-circuit | C2 -> C3 -> C4?) -> C5.

Usage:
    python -m gcqa.run --order 500119014
    python -m gcqa.run --orders orders.csv [--force] [--config config/qa.yaml]

Idempotent: an order with an existing runs/<YYYY-MM>/<order>.json is skipped
unless --force. Any unhandled per-order exception escalates that order and
never kills the run.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .adjudicate import adjudicate, needs_verifier
from .api_client import OrderNotFound, SafeguardClient
from .config import Config, load_config, load_env
from .geo import GeocodeCache, geocode_property
from .prechecks import PipelineAbort, run_prechecks
from .prefetch import prefetch, save_snapshot
from .report import write_report
from .review import ReviewResult, run_review


def month_dir(root: Path) -> Path:
    return root / "runs" / datetime.now(timezone.utc).strftime("%Y-%m")


async def process_order(order: str, client: SafeguardClient, cfg: Config,
                        run_id: str, out_dir: Path, snapshot: bool = False) -> dict:
    started = time.monotonic()
    root = cfg.root
    bundle = await prefetch(order, client, cfg, root / "cache" / "photos")

    geocode = await geocode_property(
        bundle.fields.get("address") or "", bundle.fields.get("city") or "",
        bundle.fields.get("state") or "", bundle.fields.get("zip") or "",
        cache=GeocodeCache(root / cfg.geocode.cache_path,
                           cfg.geocode.cache_ttl_days, cfg.geocode.negative_ttl_days),
        timeout_s=cfg.geocode.timeout_s, retries=cfg.geocode.retries,
        rate_limit_s=cfg.geocode.rate_limit_s,
        nominatim_enabled=cfg.geocode.nominatim_enabled,
        multi_match_radius_m=cfg.geocode.multi_match_radius_m,
    )
    signals = run_prechecks(bundle, cfg, geocode)
    if snapshot:
        save_snapshot(bundle, geocode, root / "golden" / "snapshots" / order)

    draft = ReviewResult(role="drafter")
    verifier: ReviewResult | None = None
    if signals.short_circuit is None:
        draft = await run_review(bundle, signals, cfg, "drafter")
        if draft.verdict is not None and needs_verifier(draft, signals, cfg):
            verifier = await run_review(
                bundle, signals, cfg, "verifier",
                draft_outcome=draft.verdict["outcome"],
            )
    else:
        draft.error = f"short-circuited: {signals.short_circuit_reason}"

    final = adjudicate(order, draft, verifier, signals, cfg, run_id=run_id)
    final["latency_s"] = round(time.monotonic() - started, 1)

    audit_path = out_dir / f"{order}.json"
    report_path = out_dir / f"{order}.md"
    final["audit_path"] = str(audit_path.relative_to(root))
    final["report_path"] = str(report_path.relative_to(root))
    out_dir.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(final, indent=1))
    write_report(final, bundle.fields, report_path, final["audit_path"])
    return final


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _escalation_record(final: dict) -> dict:
    drafter = final.get("drafter") or {}
    verifier = final.get("verifier") or {}
    return {
        "order_number": final["order_number"],
        "flag": final.get("flag"),
        "verifier_flag": final.get("verifier_flag"),
        "fraud_confirmed": final.get("fraud_confirmed"),
        "anomaly": final.get("anomaly"),
        "fraud_indicators": final.get("fraud_indicators") or [],
        "escalation_reason": "; ".join(final.get("adjudication", [])),
        "check_first": sorted({
            (e.get("photo_guid") or "")[:8]
            for e in (drafter.get("evidence") or []) if e.get("photo_guid")
        }),
        "drafter_rationale_plain": drafter.get("rationale_plain"),
        "verifier_rationale_plain": verifier.get("rationale_plain"),
        "report_path": final.get("report_path"),
    }


async def run_orders(orders: list[str], cfg: Config, force: bool, run_id: str,
                     snapshot: bool = False, out_dir: Path | None = None) -> list[dict]:
    root = cfg.root
    creds = load_env(root / ".env")
    sem = asyncio.Semaphore(cfg.runtime.concurrency)
    results: list[dict] = []
    # One output dir per run — pinned up front so a UTC month rollover mid-run
    # can't split the idempotency check from the write location. An explicit
    # out_dir isolates experiments (model comparisons) from the baseline records,
    # including their results/escalations streams.
    explicit_out = out_dir is not None
    out_dir = out_dir or month_dir(root)
    stream_dir = out_dir if explicit_out else (root / "runs")
    processed: set[str] = set()               # in-run dedup for repeated CSV lines

    async with SafeguardClient(creds) as client:
        async def one(order: str) -> None:
            async with sem:
                if order in processed:
                    return
                processed.add(order)
                audit = out_dir / f"{order}.json"
                if audit.exists() and not force:
                    print(f"{order}: skipped (already reviewed — use --force to re-run)")
                    return
                try:
                    final = await process_order(order, client, cfg, run_id, out_dir, snapshot)
                except OrderNotFound:
                    _append_jsonl(stream_dir / "ops_queue.jsonl",
                                  {"order_number": order, "issue": "not_found",
                                   "detail": "API 500 GetWorkOrderInfo marker", "run_id": run_id})
                    print(f"{order}: NOT FOUND (API 500 marker) — logged to ops_queue.jsonl")
                    return
                except PipelineAbort as e:
                    _append_jsonl(stream_dir / "ops_queue.jsonl",
                                  {"order_number": order, "issue": "pipeline_abort",
                                   "detail": str(e), "run_id": run_id})
                    print(f"{order}: ABORTED — {e} (logged to ops_queue.jsonl)")
                    return
                except Exception as e:
                    final = {
                        "order_number": order, "review_stage": "final",
                        "flag": None, "disposition": "human_review",
                        "outcome": None, "confidence": None,
                        "adjudication": [f"pipeline error: {type(e).__name__}: {e}"],
                        "drafter": None, "verifier": None, "agreement": None,
                        "gate_events": [], "signals": {}, "models": {},
                        "cost_usd": None, "cost_unknown": True, "session_ids": [],
                        "config_version": cfg.config_version,
                        "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "run_id": run_id, "report_path": None, "audit_path": None,
                    }
                results.append(final)
                _append_jsonl(stream_dir / "results.jsonl", final)
                if final.get("disposition") == "human_review":
                    _append_jsonl(stream_dir / "escalations.jsonl", _escalation_record(final))
                cost = final.get("cost_usd")
                cost_str = f"${cost:.3f}" if cost is not None else "cost unknown"
                print(f"{order}: {final.get('flag') or 'unclassified'} "
                      f"[{final.get('disposition')}] "
                      f"(confidence {final.get('confidence')}, {cost_str}, "
                      f"{final.get('latency_s', '?')}s)")

        await asyncio.gather(*(one(o) for o in orders))

    if results:
        summary = out_dir / f"summary-{run_id}.csv"
        with summary.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["order", "flag", "disposition", "confidence", "cost_usd", "latency_s"])
            for r in results:
                w.writerow([r["order_number"], r.get("flag"), r.get("disposition"),
                            r.get("confidence"), r.get("cost_usd"), r.get("latency_s")])
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SafeGuard grass-cut QA agent")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--order", help="single order number")
    group.add_argument("--orders", help="CSV/one-per-line file of order numbers")
    ap.add_argument("--config", default="config/qa.yaml")
    ap.add_argument("--force", action="store_true", help="re-review even if a record exists")
    ap.add_argument("--snapshot", action="store_true",
                    help="freeze OrderBundle+geocode to golden/snapshots/ for evals")
    ap.add_argument("--out-dir", default=None,
                    help="isolate this run's records/streams (experiments; default runs/<YYYY-MM>)")
    ap.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    # The Agent SDK needs ANTHROPIC_API_KEY in the process env; .env is its home here.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        dotenv = cfg.root / ".env"
        if dotenv.exists():
            for line in dotenv.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()
                    break
    if args.order:
        orders = [args.order.strip()]
    else:
        text = Path(args.orders).read_text()
        orders = [line.split(",")[0].strip() for line in text.splitlines()
                  if line.strip() and not line.startswith("#")]

    out_dir = (cfg.root / args.out_dir) if args.out_dir else None
    results = asyncio.run(run_orders(orders, cfg, args.force, args.run_id,
                                     args.snapshot, out_dir=out_dir))
    print(f"\n{len(results)} order(s) reviewed — run {args.run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
