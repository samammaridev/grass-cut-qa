"""Prompt/request assembly, shared by SDK mode and the contingent batch mode
(ORCHESTRATION.md §6 "one schema, two transports" — this module is the single
source for both the system prompt and the submit_review schema).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from .config import Config
from .prechecks import Signals
from .prefetch import OrderBundle

# The assignment's three flags — the model must ALWAYS commit to one.
# Human escalation is a routing disposition decided by code, never a flag.
FLAGS = ("good_to_pay", "gcfu", "potential_fraud")
DISPOSITIONS = ("auto", "human_review")

SUBMIT_REVIEW_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "order_number": {"type": "string"},
        "outcome": {
            "type": "string", "enum": list(FLAGS),
            "description": "The assignment flag. Always commit to your best-supported "
                           "flag — uncertainty goes in confidence/needs_human, never here.",
        },
        "confidence": {
            "type": "number",
            "description": "0-1, honest probability the flag is correct",
        },
        "needs_human": {
            "type": "boolean",
            "description": "true when a person should confirm the flag before action: "
                           "extraordinary conditions, unreadable/conflicting documentation, "
                           "genuine ambiguity. Reason goes in notes_for_human.",
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "source_type": {"type": "string",
                                    "enum": ["photo", "field", "signal", "document"]},
                    "photo_guid": {"type": ["string", "null"]},
                    "field_key": {"type": ["string", "null"]},
                    "observation": {"type": "string"},
                },
                "required": ["claim", "source_type", "observation"],
            },
        },
        "checks": {
            "type": "object",
            "properties": {
                "location_match": {"type": "string", "enum": ["pass", "fail", "unclear"]},
                "time_on_site": {"type": "string", "enum": ["pass", "fail", "unclear"]},
                "four_sides": {"type": "string", "enum": ["pass", "fail", "unclear"]},
                "before_after_diff": {"type": "string", "enum": ["pass", "fail", "unclear"]},
                "extraordinary_conditions": {"type": "boolean"},
            },
            "required": ["location_match", "time_on_site", "four_sides",
                         "before_after_diff", "extraordinary_conditions"],
        },
        "fraud_indicators": {
            "type": "array", "items": {"type": "string"},
            "description": "Every fraud-consistent anomaly observed, one short specific "
                           "string each. Does NOT change the flag — travels with it to "
                           "the human reviewer. Empty when nothing is suspicious.",
        },
        "flag_reasons": {
            "type": "array", "items": {"type": "string"},
            "description": "2-5 short bullet points stating exactly why this flag applies "
                           "(the decision rationale, one specific reason per item).",
        },
        "review_items": {
            "type": "array", "items": {"type": "string"},
            "description": "Actionable checklist for the human reviewer, in priority order — "
                           "each item names what to verify and where (photo guid / document / "
                           "signal). Empty only when nothing needs human verification.",
        },
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
        "rationale_plain": {
            "type": "string",
            "description": (
                "Plain-language rationale for a non-technical ops reviewer. 4-8 "
                "sentences, max 120 words. Restate ONLY facts already present in "
                "your evidence[] and checks — cite photo guids (8-char prefix ok). "
                "No new claims, no numerals absent from evidence/checks/"
                "DETERMINISTIC_SIGNALS, no confidence value, and do not predict "
                "the final decision (adjudication is downstream)."
            ),
        },
        "notes_for_human": {"type": ["string", "null"]},
    },
    "required": ["order_number", "outcome", "confidence", "needs_human", "evidence",
                 "checks", "fraud_indicators", "flag_reasons", "review_items",
                 "recommended_actions", "rationale_plain"],
}

# Backward-compat alias (older imports); the enum no longer contains escalation.
OUTCOMES = FLAGS


def build_system_prompt(cfg: Config, role: str) -> str:
    """role: "drafter" | "verifier". Verifier = same rules + independence suffix."""
    root = cfg.root
    prompt = (root / cfg.prompts.system).read_text()
    fewshot_dir = root / cfg.prompts.fewshots
    if fewshot_dir.is_dir():
        shots = sorted(fewshot_dir.glob("*.md"))
        if shots:
            prompt += "\n\n# Worked examples\n" + "\n\n".join(p.read_text() for p in shots)
    if role == "verifier":
        prompt += "\n\n" + (root / cfg.prompts.verifier_suffix).read_text()
    return prompt


def _model_fields(fields: dict) -> dict:
    """Strip C1-only keys (underscore prefix) — the model never sees them."""
    return {k: v for k, v in fields.items() if not k.startswith("_")}


def _photo_flag(photo, signals: Signals) -> str:
    """inlier | outlier | unknown. A photo with missing/junk GPS must read
    'unknown' — labeling it 'inlier' would assert location evidence that does
    not exist (confirmed review finding)."""
    if photo.lat is None or photo.lon is None:
        return "unknown"
    if any(o["guid"] == photo.guid for o in signals.gps.get("outliers", [])):
        return "outlier"
    if signals.gps.get("medoid") is None:
        return "unknown"
    return "inlier"


def build_user_content(bundle: OrderBundle, signals: Signals,
                       draft_outcome: str | None = None) -> list[dict]:
    """The single user message: signals + fields + every review photo with its
    metadata line. draft_outcome is appended ONLY for the verifier (never the
    drafter's reasoning, evidence, confidence, or rationale — independence).

    Token discipline: JSON blocks are compact (no indentation) and photo guids
    are 8-char prefixes (the gates accept >=8-char citations) — measured saving
    ~0.5k tokens/order at identical information content.
    """
    compact = {"separators": (",", ":")}
    blocks: list[dict] = [
        {"type": "text", "text": "DETERMINISTIC_SIGNALS:\n" + json.dumps(signals.to_dict(), **compact)},
        {"type": "text", "text": "ORDER_FIELDS:\n" + json.dumps(_model_fields(bundle.fields), **compact)},
    ]
    if bundle.documents:
        doc_parts = []
        for d in bundle.documents:
            header = f"--- document: {d.desc_text} ({d.mime}"
            if d.duplicates > 1:
                header += f", x{d.duplicates} identical copies"
            header += ", truncated)" if d.truncated else ")"
            doc_parts.append(f"{header}\n{d.text}")
        blocks.append({"type": "text", "text": "ORDER_DOCUMENTS:\n" + "\n\n".join(doc_parts)})
    taken_unreliable = bool(signals.photo_integrity.get("taken_dates_unreliable")) \
        if signals.photo_integrity else False
    for p in bundle.photos:
        gps_flag = _photo_flag(p, signals)
        taken = "UPLOAD-STAMPED (unreliable — read the on-photo watermark)" \
            if taken_unreliable else (p.taken_date or "unknown")
        meta = (
            f"photo {p.guid[:8]} | stage: {p.desc_prefix} | station: {p.desc_text} | "
            f"taken: {taken} | gps: {gps_flag}"
        )
        blocks.append({"type": "text", "text": meta})
        if p.local_path:
            data = base64.standard_b64encode(Path(p.local_path).read_bytes()).decode()
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
            })
    if draft_outcome is not None:
        blocks.append({"type": "text",
                       "text": json.dumps({"draft_outcome": draft_outcome})})
    blocks.append({"type": "text", "text": (
        f"Review work order {bundle.order_number} now, then call submit_review exactly once. "
        "Reminder: rationale_plain must be UNDER 120 words — a longer one is rejected "
        "and costs a full retry."
    )})
    return blocks
