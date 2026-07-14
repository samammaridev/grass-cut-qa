"""C3 gate rules — pure functions on the verdict JSON (ORCHESTRATION.md §11).

Transport-agnostic by design: the SDK PreToolUse hook (review.py) and the
contingent batch post-hoc validator both call evaluate_submit(); a parity test
asserts identical decisions on a frozen verdict corpus. Zero SDK or network
imports in this module.

Deny classes:
- "substantive" — protects money and vendors. Two denies of the same class
  end the session -> C5 escalates.
- "lint" — protects the narrative only. After two lint denies the verdict is
  ACCEPTED with the narrative replaced by a stub: prose formatting must never
  escalate, demote, or block a substantively valid verdict.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field

from .config import Config
from .prechecks import Signals
from .prompt_builder import FLAGS

STUB_RATIONALE = "rationale unavailable — see evidence tables"

_OUTCOME_WORDS = {
    "potential_fraud": re.compile(r"\bfraud(?:ulent)?\b", re.I),
    "good_to_pay": re.compile(r"\bgood\s+to\s+pay\b", re.I),
    "gcfu": re.compile(r"\bgcfu\b|\bfollow[- ]up order\b", re.I),
}
_NUMERAL_RE = re.compile(r"\d+(?:\.\d+)?")
_GUID_PREFIX_RE = re.compile(r"\b[0-9a-f]{8}\b", re.I)


@dataclass(frozen=True)
class Deny:
    clazz: str                      # stable class id for the two-denies rule
    kind: str                       # "substantive" | "lint"
    reason: str                     # fed back to the model verbatim


@dataclass
class GateContext:
    downloaded_guids: set[str]
    signals: Signals
    cfg: Config
    deny_counts: Counter = field(default_factory=Counter)
    lint_denies: int = 0


def _guid_known(cited: str, known: set[str]) -> bool:
    """Full guid or >=8-char prefix accepted."""
    cited = cited.lower()
    if len(cited) < 8:
        return False
    return any(g.lower().startswith(cited) or cited.startswith(g.lower()) for g in known)


def evaluate_submit(payload: dict, ctx: GateContext) -> Deny | None:
    """First failing rule wins; reasons are written as model-actionable feedback."""
    cfg = ctx.cfg

    outcome = payload.get("outcome")
    if outcome not in FLAGS:
        return Deny("schema", "substantive",
                    f"outcome must be one of the three assignment flags {FLAGS}, "
                    f"got {outcome!r}. Commit to your best-supported flag; express "
                    "uncertainty via confidence and needs_human instead.")

    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= float(confidence) <= 1.0):
        return Deny("confidence_bounds", "substantive",
                    "confidence must be a number in [0, 1].")

    evidence = payload.get("evidence") or []
    if not evidence:
        return Deny("empty_evidence", "substantive",
                    f"{outcome} requires at least one evidence item citing its source "
                    "(photo, field, or signal — a documentation gap is itself citable "
                    "evidence for gcfu).")

    for item in evidence:
        guid = item.get("photo_guid")
        if guid and not _guid_known(str(guid), ctx.downloaded_guids):
            return Deny("evidence_integrity", "substantive",
                        f"evidence cites photo_guid {guid!r} which is not in this order's "
                        "photo set — cite only photos you were shown.")

    if outcome == "potential_fraud":
        photo_cited = sum(1 for e in evidence if e.get("photo_guid"))
        need = cfg.gates.min_fraud_photo_evidence
        if photo_cited < need:
            return Deny("fraud_evidence_floor", "substantive",
                        f"potential_fraud requires >= {need} evidence items with photo_guid "
                        f"citations; you provided {photo_cited}. Cite additional independent "
                        "photo evidence, or flag gcfu with needs_human=true and describe "
                        "your suspicion in notes_for_human.")
        if cfg.gates.fraud_requires_deterministic_signal and not ctx.signals.fraud_signals:
            return Deny("fraud_corroboration", "substantive",
                        "potential_fraud requires a corroborating deterministic signal "
                        "(DETERMINISTIC_SIGNALS.fraud_signals is empty). Photo evidence alone "
                        "is not sufficient to zero a vendor's pay — flag gcfu with "
                        "needs_human=true and describe your suspicion in notes_for_human.")

    if outcome == "gcfu" and not any((e.get("observation") or "").strip() for e in evidence):
        return Deny("gcfu_area", "substantive",
                    "gcfu must name the specific out-of-standard areas in evidence "
                    "observations (e.g. 'foundation line, north side').")

    if not [r for r in (payload.get("flag_reasons") or []) if str(r).strip()]:
        return Deny("flag_reasons", "substantive",
                    "flag_reasons must state 2-5 specific reasons why this flag applies — "
                    "the human reads them as the decision rationale.")

    if cfg.report.lint:
        deny = _lint_rationale(payload, ctx)
        if deny:
            return deny
    return None


def _lint_rationale(payload: dict, ctx: GateContext) -> Deny | None:
    text = payload.get("rationale_plain") or ""
    outcome = payload["outcome"]

    words = len(text.split())
    if words > ctx.cfg.report.max_words:
        return Deny("lint_length", "lint",
                    f"rationale_plain is {words} words (max {ctx.cfg.report.max_words}). Shorten it.")

    for cited in _GUID_PREFIX_RE.findall(text):
        if not _guid_known(cited, ctx.downloaded_guids):
            return Deny("lint_unknown_guid", "lint",
                        f"rationale_plain cites photo {cited} which is not in this order's "
                        "photo set. Cite only photos from your evidence.")

    for word_outcome, pattern in _OUTCOME_WORDS.items():
        if word_outcome != outcome and pattern.search(text):
            return Deny("lint_outcome_word", "lint",
                        f"rationale_plain contains outcome language for {word_outcome!r} but "
                        f"the submitted outcome is {outcome!r}. Describe the evidence without "
                        "naming other outcomes.")

    sources = json.dumps(payload.get("evidence", [])) + json.dumps(payload.get("checks", {})) \
        + json.dumps(ctx.signals.to_dict())
    for numeral in _NUMERAL_RE.findall(text):
        if numeral not in sources:
            return Deny("lint_unsourced_number", "lint",
                        f"rationale_plain contains the number {numeral} which does not appear "
                        "in your evidence, checks, or DETERMINISTIC_SIGNALS. Remove or source it.")
    return None


# --- deny policy (per-class semantics, §11) -----------------------------------

@dataclass(frozen=True)
class GateAction:
    action: str                     # "deny" | "end_session" | "accept_with_stub"
    deny: Deny | None = None


def apply_deny_policy(ctx: GateContext, deny: Deny | None) -> GateAction:
    if deny is None:
        return GateAction("allow", None)
    if deny.kind == "lint":
        ctx.lint_denies += 1
        if ctx.lint_denies >= 2:
            return GateAction("accept_with_stub", deny)
        return GateAction("deny", deny)
    ctx.deny_counts[deny.clazz] += 1
    if ctx.deny_counts[deny.clazz] >= 2:
        return GateAction("end_session", deny)
    return GateAction("deny", deny)


def to_hook_deny(deny: Deny, hook_event_name: str = "PreToolUse") -> dict:
    """The SDK hook JSON that blocks the call and feeds the reason back."""
    return {
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "permissionDecision": "deny",
            "permissionDecisionReason": deny.reason,
        }
    }
