---
baseline_ref: 00fc6fd
created: '2026-10-14'
id: CLAWP-148
predictions:
  complexity: m
  confidence: 3
  duration_min: 120
  filled_by: agent
  pre_mortem: Remote ref missing or offline makes every add fail or slow; must fail
    open with a warning, and avoid a network fetch (read local fork/main ref only).
  prediction_id: 0246564a54f849759667bae3e80c8780
  success_criteria:
  - comparator: new id > remote max
    criterion: WHEN a task id exists only on fork/main (not in the checkout or ledger)
      THEN tasks add mints the next ordinal past it
    gradeable_signal: pytest test in tests/test_id_reservations.py with a temp git
      repo + bare remote
  - comparator: warning present
    criterion: WHEN the ledger is unreadable or no remote exists THEN a loud warning
      is emitted and the add still succeeds
    gradeable_signal: pytest asserts warning text and exit 0
priority: 5
scope:
- src/clawpm/tasks.py
- tests/test_id_reservations.py
updated: '2026-10-14'
---
# tasks add: refuse to reuse an ID present on fork/main or another ref (add-time git ls-tree check)

Sysops report (SYSOP-146 renamed SYSOP-181): a new task id can collide with a task that exists only on origin/fork main or another branch. Verified 2026-10-14: id = max(checkout scan, machine-wide ledger)+1 (tasks.py:1936-1949); no git ref is read, so tasks never minted on this machine (other clone, pre-ledger, arrived by merge) are invisible, and ledger read/write failure fails open to scan-only. Fix: before the mint, git ls-tree the tasks dirs of the project's primary remote main (fail-open with a loud warning when no remote) and raise the ordinal past the max; warn loudly when the ledger degrades. Subset overlap with CLAWP-147-004 (cross-machine) but distinct: this needs no decision.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

