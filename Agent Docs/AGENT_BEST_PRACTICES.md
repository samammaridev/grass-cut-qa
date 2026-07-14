# Agent Best Practices — SafeGuard Grass-Cut QA Agent

Practical build guideline distilled from Anthropic's "Building Effective AI Agents" whitepaper, the Claude Certified Architect exam guide, and SafeGuard's own requirements (Mike's QA prompt, Sam's review criteria, the eval-strategy notes, and the tech-stack interview).

## 1. Purpose

The agent reviews grass-cut work orders (contractor answers, timestamps, before/after photos pulled straight from the photo S3 bucket) and decides whether the work is payable, incomplete, or suspicious. Scale: ~25–30k grass cuts per month, currently costing ~$100k/month in manual audit and QA review. Goal: automate the routine reviews reliably enough that humans only see the hard cases.

## 2. Core principles

- **Start simple, scale intelligently.** Begin with a single-purpose agent that does one review well; add components only when they deliver measurable value. Simple systems are cheaper, easier to debug, and easier to trust.
- **Right model for the task.** Balance capability, speed, and cost. Vision analysis of photos needs a capable multimodal model; routing and formatting steps can use lighter, cheaper models.
- **Modular design.** Keep prompts in version-controlled config files (markdown governance layer), tools as discrete reusable modules, and review criteria separate from orchestration code — so operations can tune criteria without redeploying.
- **Few, well-described tools.** Scope each agent to only the tools its role needs; clear tool descriptions are the primary mechanism for reliable tool selection.
- **Observability from day one.** Trace every decision: which photos were analyzed, what each step concluded, token/cost per order, and why the final outcome was chosen. When an AI decision is questioned, you must be able to replay the reasoning, not just see a label.

## 3. Recommended architecture

A **sequential, auditable pipeline** (not a free-form agent): predictable transitions, clear audit trail, deterministic behavior — exactly what a payment-affecting review needs. High-control use cases call for single agents or sequential workflows, not open-ended multi-agent swarms.

1. **Orchestrator** — pulls the work order and photos, sequences the steps, aggregates results, owns error handling.
2. **Vision analysis agent** — analyzes photos only: location evidence, 4-sides coverage, before/after differences, time-on-site signals. Returns structured findings, not prose.
3. **Rules / comparison agent** — compares the vision findings against the work-order claims and the review criteria; drafts an outcome with confidence.
4. **Independent reviewer** — a fresh Claude instance with **no prior context** (it sees only the evidence and the criteria, not the drafter's reasoning). Self-review is unreliable; an independent instance catches what the first pass rationalized away.
5. **Escalation gate** — anything below the confidence threshold goes to a human with a structured handoff (order ID, findings, draft outcome, what's ambiguous).

**Use the Message Batches API when its discount outweighs the guardrails it forfeits.** There is no real-time requirement — results within hours are fine — and batch gives ~50% cost savings with a 24-hour window. But batch runs outside the agent loop: deterministic gates become post-hoc validators and in-loop deny feedback becomes correction rounds. Adopt it by explicit trigger, not by default (ORCHESTRATION.md §7 carries the pre-specified design and adoption thresholds); use `custom_id` to resubmit only failures.

## 4. Decision framework

Four outcomes — three automated, one human:

| Outcome | When | Action |
|---|---|---|
| **Good to pay** | Grass cut, property brought to standard, documentation reasonably supports meaningful work | Payment proceeds |
| **GCFU** (follow-up) | Specific areas remain out of standard (foundation/fence-line weeds, overgrowth) or visual proof of full completion is incomplete | Recommend a GCFU work order |
| **Potential fraud** | Wrong property, implausibly short time on site, or before/after photos show no meaningful work | Label, reduce pay to $0, route to Quality Review |
| **Escalate to human** | Confidence below threshold, extraordinary conditions reported by the vendor, missing/unclear documentation | Structured handoff to a reviewer |

Ground rules:

- **Evidence over claims.** Never assume work was done because the contractor said so. Verify: location (lat/long match or house number visible in a front-of-house photo), time on site, all 4 sides addressed, visible before/after difference.
- **The "good enough" standard.** Vendors are paid little and the bar is a reasonably maintained property, not perfection. Reopening every imperfect order would drive vendors away — hold the line on completeness, not cosmetics.
- **Asymmetric error costs.** Falsely accusing an honest vendor of fraud (pay zeroed, labeled a fraudster) is the most expensive error — it destroys field capacity. Keep that cell of the confusion matrix near zero, even if it means tolerating a few missed frauds. When fraud confidence is borderline, escalate rather than accuse.
- **Missing or unclear documentation is itself evidence** — factor it into the outcome (usually GCFU or escalate); never ignore it.

## 5. Prompt & output discipline

- **Explicit criteria beat vague instructions.** "Flag when after-photos show foundation-line weeds taller than X" outperforms "be conservative." Vague precision knobs ("only high-confidence findings") don't work; specific categorical criteria do.
- **Structured JSON output via tool use.** Define the outcome schema (outcome enum, confidence, evidence list, recommended actions) as a tool and force it with `tool_choice` — this guarantees schema-compliant output. Make fields nullable when evidence may be absent so the model doesn't fabricate values.
- **Validate and retry with feedback.** Schema enforcement kills syntax errors, not semantic ones. Validate outputs (e.g., outcome consistent with cited evidence); on failure, retry with the specific validation error appended. Don't retry when the needed information simply isn't in the photos — escalate instead.
- **Few-shot examples from real orders.** 2–4 examples per ambiguous pattern (borderline "good enough," extraordinary conditions, partial photo coverage), each showing the evidence, the reasoning, and the correct outcome. This is the most effective lever for consistent judgment on edge cases.

## 6. Evaluation & governance

- **Golden dataset (~200 orders).** Human-labeled: obvious passes, obvious fraud, GCFUs, and deliberately weird edge cases. Fixed set — it's the answer key. SafeGuard's historical orders with final dispositions are a ready-made source; no fresh labeling project needed.
- **Regression on every change.** Any prompt tweak, threshold change, or guardrail adjustment → re-run the exact same 200 cases. A fix that silently breaks three previously-passing cases must be caught immediately.
- **Confusion matrix as the health check.** Truth vs. agent output across all four outcomes (golden labels include cases whose correct answer is escalation). The goal isn't a perfect agent — it's an agent whose errors land in the cheap cells. Watch the "truth: pass / agent: fraud" cell above all.
- **Confidence calibration with configurable thresholds.** The agent outputs confidence with every outcome. Where the automation line sits (e.g., auto-decide ≥90%, escalate below) is an **ops-tunable config value**, not hardcoded — run the golden set at multiple thresholds to see how the matrix shifts, and recalibrate on real outcomes over time.
- **Version-controlled governance layer.** Review criteria, decision rules, thresholds, and few-shot examples live in markdown/config files under version control. Ops owns the tuning; every change is diffable and traceable to a regression run.

## 7. Context & reliability

- **Manage photo volume deliberately.** Don't dump every photograph into one context. Use the work order's photo categories/labels, process per-order (not per-batch-of-orders), and have the vision agent return trimmed structured findings — not raw descriptions — to downstream steps. Beware the "lost in the middle" effect on long inputs: lead with a key-facts summary, use explicit sections.
- **Provenance in every output.** Each finding cites its evidence (which photo, which timestamp, which field). Claim-source mapping must survive every handoff so the final explanation points to the exact evidence that drove the decision.
- **Structured errors, no silent failures.** Distinguish transient failures (S3 timeout → retry) from missing evidence (no after-photos → that's a review finding, not an error). Never swallow an error as an empty success, and never kill the whole batch on one bad order — propagate failure type, what was attempted, and partial results.
- **Escalate on ambiguity.** Conflicting evidence, extraordinary conditions, unreadable photos, policy gaps → human review with a structured handoff. An honest "I'm not sure" routed to a person is always cheaper than a confident wrong answer.
- **Deterministic guardrails for money-moving actions.** Anything that zeroes pay or changes order status should pass through programmatic checks (hooks/gates), not rely on prompt compliance alone — prompt instructions have a non-zero failure rate; code doesn't.
