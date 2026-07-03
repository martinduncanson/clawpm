---
baseline_ref: 6f652fd
complexity: l
created: '2026-06-15'
id: CLAWP-065
predictions:
  complexity: l
  confidence: 2
  duration_min: 1440
  filled_by: agent
  pre_mortem: most likely failure = scope creep merges the Node runtime into clawpm-core
    (breaks lean-core/no-daemon); mitigate by keeping workflows strictly external
    (shells the CLI), borrowing only the replay/provider PATTERNS into clawpm if at
    all
  reference_tasks:
  - CLAWP-052
  - CLAWP-056
  - CLAWP-059
  success_criteria:
  - a documented pattern + a reference workflows script that orchestrates a clawpm
    plan end-to-end (planner emit -> dispatch -> judge -> next) by shelling the clawpm
    CLI, runnable BOTH in Claude Code's Workflow tool AND as standalone Node, with
    executor leaves on local Qwen; clawpm core unchanged (no runtime merged in)
  unknowns: Whether off-harness/cron orchestration becomes a real need vs the in-harness
    agent loop staying sufficient; how much of the journal/replay to borrow into clawpm
    vs leave in workflows
priority: 8
---
# workflows as off-harness orchestrator for the clawpm loop (compose, don't merge)

DEFERRED / strategic. Use martinduncanson/workflows (F:/Git/workflows) — a model-agnostic dynamic-workflow runtime (agent/parallel/pipeline/phase, content-addressed sha256 replay, concurrency scheduler, ModelProvider seam incl. Ollama; Node, zero-dep) — as the deterministic, PORTABLE orchestrator for the clawpm loop.

PATTERN (compose, don't merge): a workflows script calls the clawpm CLI as its steps — planner emit -> `clawpm tasks dispatch` -> Stop-hook judge -> `clawpm next` -> repeat. The same script runs in Claude Code's native Workflow tool OR standalone Node (off-harness / cron) OR under Codex/Hermes/OpenClaw — identical everywhere, with executor leaves on LOCAL QWEN via the Ollama provider seam. This decouples clawpm's autonomous loop from any single hosted harness (true local-first, model-agnostic, resumable orchestration).

HARD RULE: clawpm stays the Python / filesystem-first / no-daemon SUBSTRATE (state, contracts, judge, calibration). workflows is the OPTIONAL EXTERNAL orchestrator that shells out to the clawpm CLI — never a core dependency, never merged into clawpm (a Node runtime inside clawpm-core breaks lean-core/no-daemon). Same posture as CLAWP-052 (agenticq): design-donor + optional layer.

BORROW into clawpm where cheap (separate from the compose work):
- the content-addressed journal/replay pattern — resume a half-done multi-step planner->execute run from a sha-keyed journal (longest-unchanged-prefix caches, first-changed re-runs). Stronger than crash-safe leases alone for the multi-leaf loop.
- the provider seam idea (local-Qwen execution) — already proven via the graphify localqwen wiring.

TRIGGER: a real need to run the loop OUTSIDE the hosted harness (cron/headless/local-only), or to make a long planner+execute run deterministically resumable. Until then, the harness agent loop (clawpm next + dispatch) covers it.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

