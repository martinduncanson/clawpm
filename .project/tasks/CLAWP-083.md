---
baseline_ref: ts:2026-07-04T08:55:10.290830+00:00
complexity: m
created: '2026-07-04'
id: CLAWP-083
predictions:
  approach: Add nargs=-1 to task_id argument in done/start/block/unblock/tasks state
    commands; per-task error isolation with aggregate JSON output
  confidence: 3
  duration_min: 240
  filled_by: operator
  pre_mortem: Cross-project task-id collision could corrupt state if scope isolation
    isn't applied at all 9 sites; lease sweep edges could race if not atomic
  success_criteria:
  - clawpm done 72 73 74 transitions each task with per-task result JSON; exit code
    non-zero if any fails; reflection/worklog fires per task; tests cover mixed success/failure
    batches and cross-project id safety
priority: 5
---
# Bulk state operations (varargs task ids)

Audit 2026-07-03 (CLI/UX): every state command takes a single task_id; only inbox ack accepts nargs=-1. Batch dispatch workflows (parallel_group teardown, sweeping a finished quick-fix batch) force N invocations - N lock acquisitions and N process startups.

SPEC: nargs=-1 on the id argument for done/start/block/unblock and tasks state. Per-task error isolation, aggregate JSON, honest exit code. Notes/flags (--note, --reflect-note) apply to ALL listed tasks - document that; per-task notes stay single-invocation.

## Acceptance Criteria

**Criterion 1:** clawpm done 72 73 74 (and start/block/unblock/tasks state) transitions each listed task, emitting per-task result JSON with per-task error isolation (one failure does not abort the rest)
- Evidence: operator judgment
- Pass condition: qualitative review

**Criterion 2:** Exit code non-zero if ANY transition failed; JSON reports which
- Evidence: operator judgment
- Pass condition: qualitative review

**Criterion 3:** Reflection/worklog capture fires per task exactly as single-task ops do
- Evidence: operator judgment
- Pass condition: qualitative review

**Criterion 4:** Tests cover mixed success/failure batches and cross-project id safety
- Evidence: operator judgment
- Pass condition: qualitative review

## Notes

Task status: ready for dispatch
Next: Review current implementation of done/start/block/unblock/tasks state commands
Research: Cross-project task-id isolation (memory entry available)

