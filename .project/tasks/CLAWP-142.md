---
baseline_ref: 9f90eae
created: '2026-10-08'
id: CLAWP-142
predictions:
  complexity: s
  duration_min: 60
  files_changed: 2
  filled_by: agent
priority: 5
updated: '2026-10-08'
---
# Windows inbox reads unlocked: PermissionError when a locked append lands mid-read (CLAWP-101 follow-up)

Found by the PR #88 r2 worker. On Windows, inbox reads (_read_events) are unlocked and raise PermissionError (errno 13, no winerror) when a locked append lands mid-read. retry_transient cannot catch it. The #88 race test works around it with a read-retry wrapper. Fix: take the shared read lock or make retry_transient recognise this errno, with a concurrent append/read test on Windows.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

