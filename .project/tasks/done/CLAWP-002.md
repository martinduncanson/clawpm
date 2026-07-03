---
complexity: s
created: '2026-05-15'
id: CLAWP-002
predictions:
  approach: 'Add two rows to the workflow grid (~line 643): ''Before push'' → pre-push
    self-review subagent (cite the feedback memory); ''After commit, before PR'' gets
    a second-column option for coderabbit:code-review running in parallel with codex-review.
    One-sentence note that they are deliberately redundant (different optimisation
    surfaces).'
  complexity: s
  confidence: 4
  duration_min: 20
  files_changed: 1
  files_scope:
  - skills/clawpm/SKILL.md
  filled_by: agent
  hypothesis: If clawpm SKILL.md doctrine names CodeRabbit as a peer reviewer to Codex
    at the same gate, dual-review becomes the default instead of an afterthought
  pre_mortem: 'Most likely failure: making the SKILL.md grid too prescriptive (''MUST
    run both''). Operator''s pattern is ''default to dual-review for code-bearing
    >50 LOC, skip below''. Keep the threshold flexible.'
  reference_tasks:
  - CLAWP-001
  success_criteria:
  - Workflow grid includes a pre-push self-review row
  - Workflow grid includes coderabbit:code-review at the post-commit gate as a peer
    to codex-review (not a follow-up)
  - One-sentence rationale links to the reviewer-triangle feedback memory
  - Runtime clones pick up the change via sync-runtime-clones.sh
  unknowns: Whether SKILL.md grid is the right surface or whether a separate references/review-doctrine.md
    is cleaner. Whether to add the 'skip below 50 LOC' threshold inline or just point
    to the memory.
priority: 5
---
# Update SKILL.md workflow grid: add coderabbit:code-review as parallel reviewer + pre-push self-review row



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

