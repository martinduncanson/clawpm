---
baseline_ref: a5cd2e4
created: '2026-09-22'
id: CLAWP-123
predictions:
  complexity: s
  duration_min: 60
  filled_by: agent
priority: 8
tags:
- testing
- flaky
updated: '2026-09-26'
---
# Flaky test: TestDoctorStaleBlocked::test_doctor_flags_blocked_with_done_deps (clock/tz sensitive)

## Problem

`tests/test_cascade.py::TestDoctorStaleBlocked::test_doctor_flags_blocked_with_done_deps`
fails intermittently. Observed failing identically on the `main` checkout
(not caused by PR #55/CLAWP-098) during the 2026-09-21/22 session — passed
earlier in the session, failed once local time crossed midnight, passed again
on a later run. Looks clock/timezone-sensitive rather than a real regression.

## Repro

```
python -m pytest tests/test_cascade.py::TestDoctorStaleBlocked::test_doctor_flags_blocked_with_done_deps -q
```

Failure shape: `assert child.id in sb_ids` — `sb_ids` comes back empty,
meaning doctor's `stale_blocked` detection (STALE_DAYS = 7, based on the
task's `updated` timestamp vs now) didn't flag a task the test set up via
`os.utime(child_blocked_path, (old_ts, old_ts))`. Worth checking whether the
test's `old_ts` computation and doctor's staleness comparison use consistent
timezone-aware arithmetic, and whether there's a boundary/rounding issue
right at day changes.

## Success criteria
- Root cause identified (timezone/clock arithmetic, or something else).
- Test passes reliably across multiple runs, including ones straddling local
  midnight.
- If the fix is in the test's timestamp setup rather than doctor itself, say
  so explicitly — don't paper over a real staleness-detection bug with a
  looser test.

## Priority
Low — pre-existing, not blocking, not caused by any in-flight work. File so
it's tracked rather than silently dropped.


## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

