---
baseline_ref: 468b81e
created: '2026-10-05'
id: CLAWP-135
predictions:
  complexity: s
  confidence: 3
  duration_min: 45
  files_changed: 2
  files_scope:
  - src/clawpm/taskstate_ignore.py
  - tests/test_gitignore_warning.py
  filled_by: agent
  pitfalls: allocator prefix resolution needs portfolio config at init
  predicted_iterations: 2
priority: 4
updated: '2026-10-05'
---
# taskstate_ignore probe selection: allocator-resolved prefix at init, index-aware probe, exhausted width

Follow-up to CLAWP-134 (PR #72), accepted as limitations at Codex r3 (2026-10-05, raw review in session 164fc7b9 scratchpad pr72-codex-r3.md).
1) project init probes the naive prefix, not the allocator-resolved one (a prefix collision gives CODE-B, init probes CODE-999) - misses the warning under a CODE-??? filename allowlist.
2) The probe chooses the highest number absent ON DISK; a deleted-but-unstaged tracked file is still in the index, so check-ignore suppresses it and masks a blanket .project/ ignore. Fix: exclude index members (git ls-files) when choosing.
3) When every number of the width exists (GI-000..GI-999) the loop returns an existing file; use width+1.
Also out of scope at r3: nested archived subtasks (done/archive/<parent>/<child>); allowlist shapes a prefix+width cannot mirror.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

