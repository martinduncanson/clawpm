---
baseline_ref: da5e440
created: '2026-09-02'
id: CLAWP-113
predictions:
  complexity: m
  confidence: 3
  duration_min: 120
  filled_by: agent
priority: 4
updated: '2026-09-02'
---
# Normalize legacy doubled-separator task prefixes already minted on disk

Codex P2 on PR #57 (CLAWP-096), verified 2026-09-02 and deliberately deferred out of that PR.

CLAWP-096 stopped assign_task_prefix from MINTING a doubled separator, but does nothing for a project that already reproduced the papercut. _infer_prefix_from_tasks is anchored + non-greedy, so it reads an existing CODE--000 as prefix 'CODE-' and assign_task_prefix returns that inferred value early, before any normalization. Such a project therefore keeps minting CODE--001, CODE--002 forever: the fix only helps fresh projects.

Needs an allocation path or migration that recognises the legacy prefix and mints the normalized form. The open decision is numbering continuity: switching the prefix mid-project means new tasks read CODE-001 while CODE--000 sits beside them, so the next number must be derived across BOTH spellings rather than restarting. Blast radius is existing installs, which is why it was not bolted onto PR #57.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

