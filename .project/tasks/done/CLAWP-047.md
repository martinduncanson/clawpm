---
complexity: s
created: '2026-06-10'
id: CLAWP-047
predictions:
  approach: add_task .md number parser used split('-')[1], which grabs the wrong segment
    when the prefix itself has a hyphen (arb-prd -> ARB-P), raising ValueError and
    collapsing every task to {prefix}-000. Replace with an anchored regex matching
    the trailing number (same shape the adjacent dir scan uses); subtask allocator
    already uses the safe split('-')[-1].
  confidence: 5
  duration_min: 60
  files_changed: 2
  files_scope:
  - src/clawpm/tasks.py
  - tests/test_task_id_allocation.py
  filled_by: agent
  hypothesis: If the .md scan parses the trailing number with an anchored regex, hyphenated-prefix
    projects allocate sequential IDs instead of overwriting into -000.
  pre_mortem: 'Most likely failure: a similar split-based parse elsewhere (subtask
    path) also collides -- checked, it uses [-1] and is safe.'
  success_criteria:
  - two consecutive add_task calls on a hyphenated-prefix project (arb-prd) yield
    ARB-P-000 then ARB-P-001, not two -000s, asserted by a test that fails on the
    old code
  - done/ and .progress files are counted; subtask files excluded; non-hyphenated
    prefixes unaffected; full suite green
priority: 5
---
# Auto-ID collision: hyphenated-prefix projects (ARB-P) overwrite every task into -000



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

