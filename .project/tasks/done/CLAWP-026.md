---
created: '2026-05-24'
id: CLAWP-026
predictions:
  approach: 'Add --apply (and --yes for non-interactive) flag to doctor. Refactor
    doctor''s current ''collect warnings into lists'' pattern: each warning type gets
    a paired apply_<type> function. --apply iterates collected warnings, calls apply_*
    per item. --yes skips confirmation prompts. Returns JSON with applied[] + skipped[]
    + errors[].'
  complexity: m
  confidence: 3
  duration_min: 240
  files_scope:
  - src/clawpm/cli.py
  - src/clawpm/doctor_apply.py
  - tests/test_doctor_apply.py
  filled_by: agent
  pitfalls: Some warnings (e.g. prefix collision) need operator judgment — can't auto-rename
    without consent. Need clear interactive vs --yes split.
  pre_mortem: 'Most likely failure: an --apply arm has a bug and corrupts data. Mitigation:
    each arm writes via tmp + rename; dry-run mode tested first.'
  predicted_iterations: 2
  success_criteria:
  - criterion: doctor --apply --yes runs all auto-fixable remediations without prompts
    gradeable_signal: pytest seeds 3 warning types, doctor --apply --yes resolves
      them all
  - criterion: Each remediation is independently disable-able
    gradeable_signal: --no-apply-<class> flags
  - criterion: Dry-run mode shows what would be applied without doing it
    gradeable_signal: --apply --dry-run output
priority: 5
---
# doctor --apply: per-warning auto-remediation arms

clawpm doctor currently emits warnings the operator must act on manually. --apply mode runs remediation per warning class: stale-task → suggest archive/rename, drift → fix frontmatter, prefix-collision → propose rename, stale-blocked → run cascade, unreadable → log + quarantine. Each arm is small but they multiply into paper-cut elimination.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

