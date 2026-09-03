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
updated: '2026-09-03'
---
# CLAWP-098 follow-up: decide fail-closed policy for an unreadable session ledger

Codex P1 on PR #55 (thread PRRT_kwDOSVLYYc6eZw40, sessions.py:308). When sessions.jsonl cannot be read or stat-ed, _replay logs at ERROR and returns an empty session set, so get_project_dir falls through to the cwd-independent portfolio registry and an ID-based mutator running inside a dispatched worktree mutates the MAIN checkout again - the exact CLAWP-098 corruption. Current behaviour is a DELIBERATE fail-open-with-a-marker: a hard raise out of get_project_dir would also take down read-only commands (tasks list/next/reflect share that chokepoint) over a rare, narrow corruption. Codex asked for either a genuine fail-closed or a safe local-worktree fallback. Decide between: (a) fail closed for MUTATORS only, which needs get_project_dir or its callers to carry a read/write intent it does not have today; (b) a local-worktree fallback that infers the scoped checkout from the dispatch marker when the ledger is unavailable - the mechanism _rediscover_moved_session already uses for relocated worktrees, which would narrow this surface substantially; (c) keep the marker-only fail-open and close the thread with that rationale. Option (b) looks strongest because the marker travels with the checkout and does not depend on the ledger at all.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

