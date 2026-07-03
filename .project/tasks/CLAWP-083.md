---
baseline_ref: 0b307f2
complexity: s
created: '2026-07-03'
id: CLAWP-083
predictions:
  confidence: 4
  duration_min: 90
  filled_by: agent
  pre_mortem: 'Most likely failure: per-task interactive prompts (reject rationale,
    confirm-close tier) do not compose with batches; decide policy - batch mode refuses
    tasks needing interactive input'
  success_criteria:
  - clawpm done 72 73 74 (and start/block/unblock/tasks state) transitions each listed
    task, emitting per-task result JSON with per-task error isolation (one failure
    does not abort the rest)
  - Exit code non-zero if ANY transition failed; JSON reports which
  - Reflection/worklog capture fires per task exactly as single-task ops do
  - Tests cover mixed success/failure batches and cross-project id safety
priority: 5
scope:
- src/clawpm/cli.py
- src/clawpm/tasks.py
---
# Bulk state operations (varargs task ids)

Audit 2026-07-03 (CLI/UX): every state command takes a single task_id; only inbox ack accepts nargs=-1. Batch dispatch workflows (parallel_group teardown, sweeping a finished quick-fix batch) force N invocations - N lock acquisitions and N process startups.

SPEC: nargs=-1 on the id argument for done/start/block/unblock and tasks state. Per-task error isolation, aggregate JSON, honest exit code. Notes/flags (--note, --reflect-note) apply to ALL listed tasks - document that; per-task notes stay single-invocation.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

