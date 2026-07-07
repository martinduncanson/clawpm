---
baseline_ref: 0b307f2
complexity: s
created: '2026-07-03'
id: CLAWP-085
predictions:
  confidence: 3
  duration_min: 120
  filled_by: agent
  pre_mortem: 'Most likely failure: something (rollup, reference-task suggestions,
    reflect) assumes done tasks live in done/ and silently loses archived history;
    grep every done/ path consumer before moving'
  success_criteria:
  - clawpm tasks archive --older-than 90d moves qualifying done tasks to .project/tasks/archive/
    (or per-year subdirs), excluded from default scans
  - Archived tasks remain readable (tasks show resolves them with an archived marker);
    reflection JSONLs untouched
  - Dry-run mode lists what would move; nothing is ever deleted
  - Tests cover archive + show-resolution + scan exclusion
priority: 6
scope:
- src/clawpm/tasks.py
- src/clawpm/cli.py
updated: '2026-07-07'
---
# Done-task archive/prune (tasks archive command)

Audit 2026-07-03 (CLI/UX): nothing moves old done tasks out of the hot path; done/ grows unboundedly and every list/next/reflect scan pays for it (companion to the scan-performance task, which attacks the same cost from the read side). Projects have an archived status but tasks have no archival.

SPEC: additive archive command, move-not-delete (destructive-ops doctrine), default scans skip archive/, explicit --include-archived to opt back in. Calibration corpus (reflections JSONL) is keyed by task id and lives outside the repo - unaffected. Reference-task suggestion machinery should still be able to read archived tasks when computing similarity (verify).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

