"""Golden-set runner: replays the identical C1->C5 pipeline against frozen
snapshots (no Safeguard API, frozen geocode) — only the model calls are live.

golden/golden.jsonl line format:
  {"order_number": "...", "snapshot_dir": "golden/snapshots/<order>",
   "truth": "good_to_pay|gcfu|potential_fraud|escalate_to_human",
   "truth_reasons": [...], "tags": [...], "labeler": "...", "labeled_at": "..."}
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from ..adjudicate import adjudicate, needs_verifier
from ..config import Config
from ..prechecks import run_prechecks
from ..prefetch import load_snapshot
from ..review import run_review


def load_golden(golden_path: Path) -> list[dict]:
    cases = []
    for line in golden_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(json.loads(line))
    return cases


async def run_case(case: dict, cfg: Config) -> dict:
    bundle, geocode = load_snapshot(cfg.root / case["snapshot_dir"])
    signals = run_prechecks(bundle, cfg, geocode)

    draft = None
    verifier = None
    if signals.short_circuit is None:
        draft = await run_review(bundle, signals, cfg, "drafter")
        if draft.verdict is not None and needs_verifier(draft, signals, cfg):
            verifier = await run_review(bundle, signals, cfg, "verifier",
                                        draft_outcome=draft.verdict["outcome"])
    else:
        from ..review import ReviewResult
        draft = ReviewResult(role="drafter",
                             error=f"short-circuited: {signals.short_circuit_reason}")

    final = adjudicate(case["order_number"], draft, verifier, signals, cfg)
    return {
        "order_number": case["order_number"],
        "truth": case["truth"],
        "predicted": final["flag"],
        "disposition": final["disposition"],
        "confidence": final.get("confidence"),
        "tags": case.get("tags", []),
        # §12 record shape: these are top-level for direct matrix/sweep/cost analysis
        "evidence_count": len(((final.get("drafter") or {}).get("evidence")) or []),
        "gate_events": final.get("gate_events", []),
        "cost_usd": final.get("cost_usd"),
        "latency_s": final.get("latency_s"),
        "transcript": final.get("session_ids", []),
        "final": final,
    }


async def run_golden(cfg: Config, golden_path: Path, out_dir: Path,
                     concurrency: int = 4) -> list[dict]:
    cases = load_golden(golden_path)
    sem = asyncio.Semaphore(concurrency)
    records: list[dict] = []

    async def one(case: dict) -> None:
        async with sem:
            try:
                rec = await run_case(case, cfg)
            except Exception as e:  # one broken snapshot never kills the eval
                rec = {"order_number": case["order_number"], "truth": case["truth"],
                       "predicted": "error", "confidence": None,
                       "tags": case.get("tags", []), "error": f"{type(e).__name__}: {e}"}
            records.append(rec)

    await asyncio.gather(*(one(c) for c in cases))
    records.sort(key=lambda r: r["order_number"])

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"eval-{stamp}.json"
    out_path.write_text(json.dumps(records, indent=1))
    return records
