---
complexity: l
created: '2026-06-11'
id: CLAWP-055
predictions:
  complexity: l
  confidence: 3
  duration_min: 360
  files_changed: 8
  filled_by: agent
  pre_mortem: most likely failure = git-coupling leaks into the schema, breaking the
    knowledge-work/project-agnostic use case; mitigate by designing baseline_ref as
    an opaque marker with a git provider + a non-git fallback from the start
  reference_tasks:
  - CLAWP-047
  - CLAWP-016
  success_criteria:
  - every task records a baseline_ref at creation (git short-SHA when in a git repo,
    else a timestamp/content marker); shown in task detail
  - a pre-dispatch reconciliation step detects when in-scope paths changed since baseline_ref
    and blocks silent dispatch on a stale task, offering reconcile/confirm (covered
    by a test simulating post-baseline edits)
priority: 5
---
# Per-task 'specified-against' baseline + pre-dispatch drift reconciliation

A task filed days ago against since-changed code/inputs is the core failure mode of a resume-across-sessions PM layer, and clawpm currently catches drift only at doctor level (fs-vs-state, commits-since-worklog) — never per-task. Stamp each task with the baseline it was specified against (git short-SHA in a code repo; a content hash / timestamp for non-code knowledge work — keep it abstract via a 'baseline_ref' field, NOT git-only). Add a pre-dispatch check that diffs the task's scope against HEAD/current-baseline and, on drift, routes to reconcile-or-confirm before an executor touches anything. Synthesises shadcn/improve's per-plan 'Planned at <SHA>' + executor drift-check step with GSD-Pi's reconcile-before-dispatch model: typed, machine-actionable DriftRecords with idempotent repairs, capped at 2 reconcile passes, persistent/irreparable drift escalates as a blocker. Reuse, do not duplicate, the existing doctor drift machinery. Project-agnostic: 'baseline_ref' + 'scope' are domain-neutral.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

