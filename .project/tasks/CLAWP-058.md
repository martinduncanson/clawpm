---
complexity: m
created: '2026-06-11'
id: CLAWP-058
predictions:
  complexity: m
  confidence: 2
  duration_min: 240
  files_changed: 6
  filled_by: agent
  pre_mortem: most likely failure = built before there is real pull for it, adding
    surface that bloats the local-first core; mitigate by keeping it deferred until
    CLAWP-056 lands and the need is demonstrated, and shipping it optional/env-gated
  reference_tasks:
  - CLAWP-056
  - CLAWP-052
  success_criteria:
  - an explicit opt-in flag publishes a task or task-tree as GitHub issue(s)/PR using
    the task's existing self-contained body; clawpm runs identically when the flag
    is unused
  unknowns: Whether one-way push suffices or bidirectional state sync is wanted; per-leaf
    issues vs one tracking issue per tree; relationship to the agenticq bridge (CLAWP-052)
    if outward sync generalises beyond GitHub
priority: 8
---
# Tasks/task-trees -> GitHub issues|PR distribution (DEFERRED, future consideration)

DEFERRED — future consideration, not for near-term build. Distribute clawpm tasks/task-trees outward to where execution work lives: as GitHub issues and/or a PR (e.g. one issue per leaf, or a tracking issue per task-tree with a checklist). Precedent: shadcn/improve `--issues`, github/spec-kit `/speckit.taskstoissues` — both publish the self-contained task body as an issue so any agent or human picks it up where work already happens.

WHY DEFERRED: this is DISTRIBUTION, not planning/ideation — tangential to the CLAWP-056 pipeline's core value. clawpm is filesystem-first / no-daemon by design, and outward sync to a remote tracker brushes the deliberately-parked agenticq-bridge scope (CLAWP-052). Revisit once the decomposition pipeline (CLAWP-056) has landed and there is a real pull for surfacing clawpm work in GitHub. Keep any eventual implementation OPTIONAL and env-gated (clawpm runs identically with it off), same posture as CLAWP-052.

DESIGN NOTES when revisited: self-containment of the emitted task body is the enabling property (the issue needs no edits to make sense to whoever picks it up) — reuse the same body the pipeline already produces. Gate behind an explicit flag (the flag IS the authorisation to create issues, per improve's rule). Map clawpm done/state back from issue close if bidirectional, but one-way push is the cheaper first cut.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

