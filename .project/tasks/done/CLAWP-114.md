---
baseline_ref: 1f4316f
created: '2026-09-03'
id: CLAWP-114
predictions:
  complexity: m
  confidence: 3
  duration_min: 90
  filled_by: agent
  success_criteria:
  - A decision is recorded with its rationale, and either the chosen behaviour is
    implemented with a regression test that fails against the current fail-open, or
    the thread is closed with the documented tradeoff
priority: 5
updated: '2026-10-07'
---
# CLAWP-098 follow-up: decide fail-closed policy for an unreadable session ledger

Codex P1 on PR #55 (thread PRRT_kwDOSVLYYc6eZw40, sessions.py:308). When sessions.jsonl cannot be read or stat-ed, _replay logs at ERROR and returns an empty session set, so get_project_dir falls through to the cwd-independent portfolio registry and an ID-based mutator running inside a dispatched worktree mutates the MAIN checkout again - the exact CLAWP-098 corruption. Current behaviour is a DELIBERATE fail-open-with-a-marker: a hard raise out of get_project_dir would also take down read-only commands (tasks list/next/reflect share that chokepoint) over a rare, narrow corruption. Codex asked for either a genuine fail-closed or a safe local-worktree fallback. Decide between: (a) fail closed for MUTATORS only, which needs get_project_dir or its callers to carry a read/write intent it does not have today; (b) a local-worktree fallback that infers the scoped checkout from the dispatch marker when the ledger is unavailable - the mechanism _rediscover_moved_session already uses for relocated worktrees, which would narrow this surface substantially; (c) keep the marker-only fail-open and close the thread with that rationale. Option (b) looks strongest because the marker travels with the checkout and does not depend on the ledger at all.

## Acceptance Criteria

- [x] With sessions.jsonl unreadable (read fault, stat fault, or content with zero valid events), an ID-based mutator run from inside a worktree carrying a dispatch marker for the same project mutates the worktree's task file, not the main checkout's (tests/test_clawp114_unreadable_ledger_fallback.py; fails on the previous code).
- [x] No marker (main checkout): behaviour unchanged, read-only commands still work, ERROR log kept.
- [x] A marker naming another project is never matched (project isolation).
- [x] Every degraded path leaves a trace: the original ERROR logs stay, and the fallback adds a WARNING naming the worktree, task and project.

## Decision (2026-10-07)

Option (b) chosen. The dispatch marker travels with the checkout, needs no ledger, and narrows the corruption surface to "ledger unavailable AND no marker", which is the main checkout where registry resolution is correct anyway. (a) rejected: get_project_dir has no read/write intent, so fail-closed-for-mutators means plumbing intent through every caller (large change, same chokepoint risk). (c) rejected: it leaves a known silent main-checkout mutation. Note: `sessions._rediscover_moved_session` was removed from PR #55 (see CLAWP-117), so the marker walk is new code (`sessions._marker_fallback_session`) using `dispatch.read_dispatch_marker`. An inconsistent marker never raises: another project's marker is skipped (a legitimate cross-project lookup), an unreadable one is logged and skipped. A raise would take down read-only commands sharing this chokepoint.

## Notes

