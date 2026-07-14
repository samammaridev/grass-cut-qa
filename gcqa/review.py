"""C2 drafter / C4 verifier: one fresh Agent SDK session per order.

The verdict channel is the in-process MCP tool submit_review; the C3 gate runs
as a PreToolUse hook whose deny reason is fed back to the model in-loop
(ORCHESTRATION.md §6/§7). Hooks here are thin adapters over the pure rules in
gates.py — the logic itself is transport-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from .config import Config
from .gates import STUB_RATIONALE, GateContext, apply_deny_policy, evaluate_submit, to_hook_deny
from .prechecks import Signals
from .prefetch import OrderBundle
from .prompt_builder import SUBMIT_REVIEW_JSON_SCHEMA, build_system_prompt, build_user_content

SUBMIT_TOOL = "mcp__qa__submit_review"


@dataclass
class ReviewResult:
    role: str
    verdict: dict | None = None
    subtype: str | None = None
    cost_usd: float = 0.0
    session_id: str | None = None
    gate_events: list[dict] = field(default_factory=list)
    stubbed_narrative: bool = False
    error: str | None = None
    usage: dict | None = None                 # real token counts from ResultMessage
    num_turns: int | None = None


class _SessionState:
    def __init__(self, gate_ctx: GateContext, result: ReviewResult):
        self.gate_ctx = gate_ctx
        self.result = result
        self.ended_by_gate = False


def _make_server(state: _SessionState):
    @tool(
        "submit_review",
        "Submit your final QA verdict for this work order. Call exactly once; "
        "the verdict is validated by deterministic gates before acceptance.",
        SUBMIT_REVIEW_JSON_SCHEMA,
    )
    async def submit_review(args: dict[str, Any]) -> dict[str, Any]:
        # Reached only if the PreToolUse gate allowed the call.
        state.result.verdict = args
        return {"content": [{"type": "text", "text": "Verdict recorded."}]}

    return create_sdk_mcp_server(name="qa", version="1.0.0", tools=[submit_review])


def _make_hooks(state: _SessionState) -> dict:
    async def gate_submit(input_data: dict, tool_use_id, context) -> dict:
        payload = input_data.get("tool_input") or {}
        deny = evaluate_submit(payload, state.gate_ctx)
        action = apply_deny_policy(state.gate_ctx, deny)
        if action.action == "allow":
            return {}
        state.result.gate_events.append(
            {"class": action.deny.clazz, "kind": action.deny.kind,
             "action": action.action, "reason": action.deny.reason}
        )
        if action.action == "accept_with_stub":
            state.result.stubbed_narrative = True
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": {**payload, "rationale_plain": STUB_RATIONALE},
                }
            }
        if action.action == "end_session":
            state.ended_by_gate = True
            out = to_hook_deny(action.deny)
            out["hookSpecificOutput"]["permissionDecisionReason"] += (
                " This rule has now failed twice — the order will be escalated to a human."
            )
            out["continue_"] = False
            return out
        return to_hook_deny(action.deny)

    async def audit_submit(input_data: dict, tool_use_id, context) -> dict:
        state.result.gate_events.append(
            {"class": "accepted", "kind": "audit", "action": "accepted",
             "reason": "verdict accepted by gates"}
        )
        # The verdict is recorded — end the session here. Without this the model
        # writes a wrap-up text turn nobody reads (measured live: ~2.2k cache-write
        # + 340 output tokens per stage, ~$0.02/order of pure waste).
        return {"continue_": False, "stopReason": "verdict accepted"}

    return {
        "PreToolUse": [HookMatcher(matcher=SUBMIT_TOOL, hooks=[gate_submit])],
        "PostToolUse": [HookMatcher(matcher=SUBMIT_TOOL, hooks=[audit_submit])],
    }


async def run_review(
    bundle: OrderBundle,
    signals: Signals,
    cfg: Config,
    role: str,                                # "drafter" | "verifier"
    draft_outcome: str | None = None,         # verifier only — never the drafter's reasoning
) -> ReviewResult:
    result = ReviewResult(role=role)
    gate_ctx = GateContext(
        downloaded_guids={p.guid for p in bundle.photos},
        signals=signals,
        cfg=cfg,
    )
    state = _SessionState(gate_ctx, result)

    options = ClaudeAgentOptions(
        model=cfg.models[role],
        system_prompt=build_system_prompt(cfg, role),
        mcp_servers={"qa": _make_server(state)},
        allowed_tools=[SUBMIT_TOOL],
        tools=[],                              # no built-ins — the review needs none
        strict_mcp_config=True,
        setting_sources=[],
        hooks=_make_hooks(state),
        max_turns=cfg.runtime.max_turns,
        max_budget_usd=cfg.runtime.max_budget_usd_per_order,
        max_thinking_tokens=cfg.runtime.max_thinking_tokens,  # runaway guard, not a squeeze
        permission_mode="default",
    )

    # Build the content EAGERLY: an exception inside the async prompt generator
    # (unreadable photo file, encoding error) is swallowed by the SDK's stream
    # consumer and surfaces as an opaque hang/timeout (review finding).
    try:
        content = build_user_content(bundle, signals, draft_outcome)
    except Exception as e:
        result.error = f"prompt build failed: {type(e).__name__}: {e}"
        return result

    async def prompt_stream():
        yield {"type": "user", "message": {"role": "user", "content": content}}

    try:
        async for message in query(prompt=prompt_stream(), options=options):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                servers = message.data.get("mcp_servers", [])
                bad = [s for s in servers if s.get("status") != "connected"]
                if bad:
                    result.error = f"MCP server(s) not connected: {bad}"
                    break
            elif isinstance(message, ResultMessage):
                result.subtype = message.subtype
                result.cost_usd = message.total_cost_usd or 0.0
                result.session_id = message.session_id
                result.usage = dict(message.usage) if message.usage else None
                result.num_turns = message.num_turns
    except Exception as e:  # any SDK failure -> escalate path, never a crash
        result.error = f"{type(e).__name__}: {e}"

    # Common post-processing — runs on EVERY path (early returns here previously
    # skipped the gate-exhaustion reset and stub replacement; review finding).
    if state.ended_by_gate:
        result.verdict = None                  # gate-exhausted -> C5 escalates
    if result.verdict is None and result.error is None:
        result.error = f"no accepted submit_review (subtype={result.subtype})"
    if result.verdict is not None and result.stubbed_narrative:
        result.verdict["rationale_plain"] = STUB_RATIONALE
    return result


def review_result_summary(r: ReviewResult) -> dict:
    v = r.verdict or {}
    return {
        "role": r.role,
        "outcome": v.get("outcome"),
        "needs_human": v.get("needs_human"),
        "confidence": v.get("confidence"),
        "evidence": v.get("evidence"),
        "checks": v.get("checks"),
        "fraud_indicators": v.get("fraud_indicators"),
        "flag_reasons": v.get("flag_reasons"),
        "review_items": v.get("review_items"),
        "recommended_actions": v.get("recommended_actions"),
        "rationale_plain": v.get("rationale_plain"),
        "notes_for_human": v.get("notes_for_human"),
        "subtype": r.subtype,
        "cost_usd": round(r.cost_usd, 4),
        "session_id": r.session_id,
        "gate_events": r.gate_events,
        "error": r.error,
        "usage": r.usage,
        "num_turns": r.num_turns,
    }
