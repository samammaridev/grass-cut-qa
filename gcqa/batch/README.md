# batch/ — contingent Message Batches execution mode (NOT built)

Inert until the ORCHESTRATION.md §7 trigger fires (sustained model spend
> $5k/mo, or Haiku-parity failure while spend ≥ $2k/mo) and the certification
preconditions pass.

When triggered, this package gains: a request builder that byte-reproduces
`prompt_builder` output as raw Messages-API batch requests with forced
`tool_choice` on `submit_review`; a poller; and an idempotent per-order state
machine (`custom_id = {order}:{stage}:{round}`, max 2 correction rounds,
cancel-to-sync at T+6 h). The gate rules stay in `gcqa/gates.py` — shared with
the SDK hook path and pinned by `tests/test_gates.py::test_gate_parity_corpus`.
