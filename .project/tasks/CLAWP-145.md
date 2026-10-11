---
baseline_ref: bf52f5f
created: '2026-10-11'
id: CLAWP-145
priority: 5
updated: '2026-10-11'
---
# CLAWP-142 stress test: force a reader/writer overlap

Codex r1 P3 on PR #97: the inbox stress test does not guarantee the reader overlaps a locked append, so it can pass without exercising the retry path. Use a barrier/event so the reader starts while the writer holds the byte-0 lock.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

