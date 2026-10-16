---
baseline_ref: 1119ac2
created: '2026-10-16'
id: CLAWP-159
predictions:
  complexity: s
  confidence: 3
  duration_min: 90
  filled_by: agent
  pre_mortem: 'Most likely failure: guarding resolve() but then using a None target
    downstream; mitigate by deciding fall-back vs clean error per site and testing
    each'
  prediction_id: 2b50045bfaae4326b305146ae0995a4f
  success_criteria:
  - WHEN a --target-dir path whose resolve() raises OSError is passed to lease and
    to dispatch THEN each command warns and falls back or exits with a clean error,
    not a traceback
priority: 7
updated: '2026-10-16'
---
# Guard remaining unguarded Path.resolve() on --target-dir paths (CLAWP-136 follow-up)

CLAWP-136 item 1 is fixed (dispatch --target-dir routes through resolve_scope -> resolve_path_or_none, test: tests/test_explicit_scope.py TestDispatchTargetDirResolveGuard). The existing test fails only the FIRST resolve, so it misses other unguarded target_dir resolves: src/clawpm/cli/lease.py:101, src/clawpm/cli/tasks.py:2648 (--lease-ttl path), src/clawpm/dispatch.py:131,155,488,665. Same class as PR #75 Codex r1 P2 #2 (OSError on an unavailable target crashes the command). Re-grep first; line numbers are from 2026-10-16.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

