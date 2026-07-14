"""REAL Message Batches run of the drafter+verifier waves — the ORCHESTRATION §7
contingent design, actually executed and billed at batch rates (50% discount).

Usage: .venv/bin/python -m gcqa.batch.compare --orders orders10.txt

One schema, two transports: this reuses the exact prompt_builder output and
SUBMIT_REVIEW_JSON_SCHEMA the SDK path uses, and validates every returned
verdict with the same gates.evaluate_submit (post-hoc; denies are recorded,
not corrected in this run — per §7 corrections would run sync).

Note: forced tool_choice is API-incompatible with thinking, so we use
tool_choice auto + an explicit call instruction, with adaptive thinking at
medium effort for reasoning parity with the SDK path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from ..adjudicate import decide, needs_verifier
from ..api_client import SafeguardClient
from ..config import Config, load_config, load_env
from ..gates import GateContext, evaluate_submit
from ..geo import GeocodeCache, geocode_property
from ..prechecks import Signals, run_prechecks
from ..prefetch import OrderBundle, prefetch
from ..prompt_builder import (
    SUBMIT_REVIEW_JSON_SCHEMA,
    build_system_prompt,
    build_user_content,
)
from ..review import ReviewResult

# $/MTok at BATCH rates (50% off), intro Sonnet-5 pricing active through 2026-08-31
BATCH_PRICES = {
    "claude-sonnet-5": {"in": 1.00, "out": 5.00},
    "claude-opus-4-8": {"in": 2.50, "out": 12.50},
    "claude-haiku-4-5": {"in": 0.50, "out": 2.50},
}
MAX_TOKENS = 8000
POLL_INTERVAL_S = 30
POLL_CAP_S = 14400  # batch queues vary; 45 min proved too tight live

TOOL = {
    "name": "submit_review",
    "description": "Submit your final QA verdict for this work order. Call exactly once.",
    "input_schema": SUBMIT_REVIEW_JSON_SCHEMA,
}


def build_request(custom_id: str, model: str, system: str, content: list[dict]) -> dict:
    return {
        "custom_id": custom_id,
        "params": {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "tools": [TOOL],
            "tool_choice": {"type": "auto"},
            # Claude 5 family: adaptive thinking + effort (the legacy
            # enabled/budget_tokens shape is rejected by these models)
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "medium"},
            "messages": [{"role": "user", "content": content}],
        },
    }


def batch_cost(model: str, usage) -> float:
    p = BATCH_PRICES[model]
    inp = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    return (inp * p["in"] + (usage.output_tokens or 0) * p["out"]) / 1e6


def extract_verdict(message) -> dict | None:
    for block in message.content:
        if block.type == "tool_use" and block.name == "submit_review":
            return block.input
    return None


def submit_and_wait(client: anthropic.Anthropic, requests: list[dict], label: str) -> dict:
    """Create a REAL batch, poll to completion, return {custom_id: result}."""
    batch = client.messages.batches.create(requests=requests)
    print(f"[{label}] batch {batch.id} submitted ({len(requests)} requests) — polling")
    started = time.monotonic()
    while batch.processing_status == "in_progress":
        if time.monotonic() - started > POLL_CAP_S:
            raise TimeoutError(f"batch {batch.id} still in_progress after {POLL_CAP_S}s")
        time.sleep(POLL_INTERVAL_S)
        batch = client.messages.batches.retrieve(batch.id)
    elapsed = time.monotonic() - started
    print(f"[{label}] batch {batch.id} {batch.processing_status} in {elapsed:.0f}s "
          f"(counts: {batch.request_counts})")
    results = {}
    for entry in client.messages.batches.results(batch.id):
        results[entry.custom_id] = entry.result
    return {"batch_id": batch.id, "elapsed_s": round(elapsed, 1), "results": results}


async def gather_inputs(orders: list[str], cfg: Config) -> dict[str, tuple[OrderBundle, Signals]]:
    creds = load_env(cfg.root / ".env")
    out: dict[str, tuple[OrderBundle, Signals]] = {}
    async with SafeguardClient(creds) as client:
        for order in orders:  # photo cache is warm from the SDK runs — this is fast
            bundle = await prefetch(order, client, cfg, cfg.root / "cache" / "photos")
            geocode = await geocode_property(
                bundle.fields.get("address") or "", bundle.fields.get("city") or "",
                bundle.fields.get("state") or "", bundle.fields.get("zip") or "",
                cache=GeocodeCache(cfg.root / cfg.geocode.cache_path,
                                   cfg.geocode.cache_ttl_days, cfg.geocode.negative_ttl_days),
                timeout_s=cfg.geocode.timeout_s, retries=cfg.geocode.retries,
                rate_limit_s=cfg.geocode.rate_limit_s,
                nominatim_enabled=cfg.geocode.nominatim_enabled,
                multi_match_radius_m=cfg.geocode.multi_match_radius_m,
            )
            signals = run_prechecks(bundle, cfg, geocode)
            out[order] = (bundle, signals)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="REAL Batch API run + cost comparison")
    ap.add_argument("--orders", default="orders10.txt")
    ap.add_argument("--config", default="config/qa.yaml")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        for line in (cfg.root / ".env").read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()
    client = anthropic.Anthropic()

    orders = [line.split(",")[0].strip() for line in Path(args.orders).read_text().splitlines()
              if line.strip() and not line.startswith("#")]
    print(f"gathering inputs for {len(orders)} orders (Safeguard API, cached photos)")
    inputs = asyncio.run(gather_inputs(orders, cfg))

    drafter_model = cfg.models["drafter"]
    verifier_model = cfg.models["verifier"]
    system_drafter = build_system_prompt(cfg, "drafter")
    system_verifier = build_system_prompt(cfg, "verifier")

    # ---- Wave 1: drafts (one REAL batch) ----
    wave1_reqs = [
        build_request(f"{order}-draft-0", drafter_model, system_drafter,
                      build_user_content(bundle, signals))
        for order, (bundle, signals) in inputs.items()
    ]
    wave1 = submit_and_wait(client, wave1_reqs, "wave1-draft")

    per_order: dict[str, dict] = {o: {"order": o} for o in orders}
    verifier_needed: dict[str, str] = {}
    for order, (bundle, signals) in inputs.items():
        entry = wave1["results"].get(f"{order}-draft-0")
        rec = per_order[order]
        if entry is None or entry.type != "succeeded":
            rec["draft"] = None
            rec["draft_status"] = entry.type if entry else "missing"
            continue
        msg = entry.message
        verdict = extract_verdict(msg)
        rec["draft"] = verdict
        rec["draft_status"] = "succeeded"
        rec["draft_cost_usd"] = round(batch_cost(drafter_model, msg.usage), 4)
        rec["draft_usage"] = {"in": msg.usage.input_tokens, "out": msg.usage.output_tokens}
        if verdict:
            ctx = GateContext(downloaded_guids={p.guid for p in bundle.photos},
                              signals=signals, cfg=cfg)
            deny = evaluate_submit(verdict, ctx)
            rec["gate"] = deny.clazz if deny else "pass"
            draft_rr = ReviewResult(role="drafter", verdict=verdict)
            if needs_verifier(draft_rr, signals, cfg):
                verifier_needed[order] = verdict["outcome"]
        else:
            rec["gate"] = "no_tool_call"

    # ---- Wave 2: verifiers (one REAL batch, only where mandated) ----
    wave2 = None
    if verifier_needed:
        wave2_reqs = [
            build_request(f"{order}-verify-0", verifier_model, system_verifier,
                          build_user_content(inputs[order][0], inputs[order][1],
                                             draft_outcome=outcome))
            for order, outcome in verifier_needed.items()
        ]
        wave2 = submit_and_wait(client, wave2_reqs, "wave2-verify")
        for order in verifier_needed:
            entry = wave2["results"].get(f"{order}-verify-0")
            rec = per_order[order]
            if entry is None or entry.type != "succeeded":
                rec["verify"] = None
                rec["verify_status"] = entry.type if entry else "missing"
                continue
            msg = entry.message
            rec["verify"] = extract_verdict(msg)
            rec["verify_status"] = "succeeded"
            rec["verify_cost_usd"] = round(batch_cost(verifier_model, msg.usage), 4)

    # ---- Adjudicate (same decision function, same thresholds) ----
    for order, (bundle, signals) in inputs.items():
        rec = per_order[order]
        flag, disposition, confidence, adjudication = decide(
            rec.get("draft"), rec.get("verify"),
            signals.short_circuit_reason if signals.short_circuit else None,
            cfg.thresholds.auto_decide, cfg.gates.fraud_requires_verifier_agreement,
            draft_error=rec.get("draft_status"),
        )
        if order in verifier_needed and rec.get("verify") is None:
            disposition = "human_review"
            adjudication.append("mandated verifier failed in batch — human review")
        indicators = [f"deterministic: {s}" for s in signals.fraud_signals]
        for v in (rec.get("draft"), rec.get("verify")):
            if v:
                for i in (v.get("fraud_indicators") or []):
                    if i and i not in indicators:
                        indicators.append(i)
        rec["flag"] = flag
        rec["disposition"] = disposition
        rec["anomaly"] = bool(indicators)
        rec["fraud_indicators"] = indicators
        rec["final_outcome"] = flag              # legacy alias for older tooling
        rec["confidence"] = confidence
        rec["adjudication"] = adjudication
        rec["total_cost_usd"] = round(
            (rec.get("draft_cost_usd") or 0) + (rec.get("verify_cost_usd") or 0), 4)

    doc = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "models": {"drafter": drafter_model, "verifier": verifier_model},
        "config_version": cfg.config_version,
        "wave1": {"batch_id": wave1["batch_id"], "elapsed_s": wave1["elapsed_s"]},
        "wave2": ({"batch_id": wave2["batch_id"], "elapsed_s": wave2["elapsed_s"]}
                  if wave2 else None),
        "orders": list(per_order.values()),
        "total_cost_usd": round(sum(r["total_cost_usd"] for r in per_order.values()), 4),
    }
    out_path = cfg.root / "runs" / "batch_compare.json"
    out_path.write_text(json.dumps(doc, indent=1))

    print(f"\n{'order':12} {'batch outcome':20} {'conf':5} {'gate':22} {'cost':8}")
    for r in per_order.values():
        print(f"{r['order']:12} {r['final_outcome']:20} "
              f"{r['confidence'] if r['confidence'] is not None else '-':5} "
              f"{r.get('gate','-'):22} ${r['total_cost_usd']:.4f}")
    print(f"\nTOTAL batch cost: ${doc['total_cost_usd']:.4f} "
          f"(wave1 {wave1['elapsed_s']}s" + (f", wave2 {wave2['elapsed_s']}s" if wave2 else "") + ")")
    print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
