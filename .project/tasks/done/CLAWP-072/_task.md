---
baseline_ref: 0b307f2
children:
- CLAWP-072-001
- CLAWP-072-002
- CLAWP-072-003
- CLAWP-072-004
- CLAWP-072-005
- CLAWP-072-006
complexity: s
created: '2026-07-03'
id: CLAWP-072
predictions:
  approach: One PR bundling five xs fixes, each a subtask; all confirmed defects from
    the 2026-07-03 four-agent audit
  confidence: 4
  duration_min: 120
  filled_by: agent
  pre_mortem: 'Most likely failure: the doctor --project passthrough touches project
    resolution and needs more than a flag add'
  success_criteria:
  - All five subtasks done; full pytest suite passes; single PR merged on fork
priority: 3
updated: '2026-07-10'
---
# Quick-fix batch: dead code, config, CLI ergonomics (audit 2026-07-03)

Umbrella for the confirmed-defect quick fixes from the 2026-07-03 audit (code-health + CLI/UX + tests + docs agents). Each defect is a subtask with its own spec. Ship as ONE PR through codex-review.

Already done inline during the audit session (not subtasks): .project/SPEC.md written (was init template stub), .project/notes/ created (was a dead CLAUDE.md pointer), mcps/ added to .gitignore (harness cache junk).

OUT OF SCOPE: SKILL.md reconciliation, CI, serve.py, decomposition - separate tasks.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

