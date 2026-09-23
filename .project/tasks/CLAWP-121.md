---
baseline_ref: 28baf54
complexity: m
created: '2026-09-18'
id: CLAWP-121
predictions:
  approach: Reserve each task-less sibling's full[:n] stripped-candidate chain in
    _portfolio_prefixes' used set (grok-4.6's suggestion), OR assign task-less siblings'
    prefixes in one deterministic global pass instead of N independent calls -- weigh
    both, the global-pass option may carry lower regression risk given this function's
    history (CLAWP-096/119/120).
  complexity: m
  confidence: 3
  files_changed: 2
  files_scope:
  - src/clawpm/tasks.py
  - tests/test_task_id_allocation.py
  filled_by: agent
  hypothesis: If _portfolio_prefixes reserves each task-less sibling's full stripped-candidate
    chain (or prefixes are assigned in one deterministic pass) instead of just the
    5-char placeholder, then two siblings sharing a 6+ char stem can no longer independently
    mint the same extended prefix.
  pre_mortem: 'Most likely failure: the full[:n] reservation approach re-introduces
    a false-collision-REPORT regression (the exact bug class CLAWP-096/119/120 spent
    9+ rounds fixing) by reserving candidates a sibling would never actually claim.'
  reference_tasks:
  - CLAWP-120
  - CLAWP-096
  - CLAWP-119
  success_criteria:
  - New regression test using genuinely-colliding stems (e.g. code-quorum/code-quiz)
    reproduces the collision on main, then passes after the fix
  - Full test suite passes (1551+ tests) with no new false-collision regressions in
    the existing prefix-collision tests
  unknowns: Whether a global deterministic pass is feasible without a larger refactor
    of assign_task_prefix's call sites (doctor's per-project loop, tasks add's single-mint
    path).
priority: 3
tags:
- task-id-allocation
updated: '2026-09-23'
---
# Fix naive-placeholder prefix collision between independently-assigned task-less siblings

`_portfolio_prefixes` (`src/clawpm/tasks.py:1317`) reserves only the 5-char
naive placeholder (`_naive_prefix_placeholder`) in `used` for a task-less
sibling — not the full chain of extended candidates that project could still
grow into. Two task-less siblings whose ids share more than 5 characters
(grok-4.6's repro: `code-quorum` and `code-quiz`, both reduce to `CODE`) each
independently compute the SAME extended candidate (`CODE-Q`) when their
prefixes are assigned via separate, independent `assign_task_prefix` calls —
exactly what `clawpm doctor` does, one project at a time via its
prefix-collision loop (`cli/project.py:784-828`), as opposed to sequential
`clawpm tasks add` calls (where the second call sees the first's REAL minted
prefix already in `used` and naturally extends past it).

This is a genuine id-uniqueness bug, not a false-collision-REPORT bug (that
class was CLAWP-120's fix, PR #57). Converged HIGH from two independent
code-quorum reviewers on PR #57 (antigravity + grok-4.6); relayed as-reported
from the manifest at `session-notes/2026-09-18-08-21-42-...-handoff.md`
(gbrain-martin project page has the fuller review context) — not yet
independently re-verified against the live code by a fresh repro. Do that
verification as this task's first step before designing the fix.

Existing regression test `test_doctor_keys_a_taskless_sibling_under_what_the_allocator_mints`
(`tests/test_task_id_allocation.py`) uses `code-quorum`/`code-runner` — those
differ at character 6 (`CODE-Q` vs `CODE-R`... actually `code-runner` ->
`CODE-R`), so it does NOT exercise this collision. A new test needs stems
that genuinely collide past the 5-char boundary, e.g. `code-quorum` /
`code-quiz` (both -> `CODE`, both would naturally extend to `CODE-Q`).

grok-4.6's suggested fix (not yet vetted): reserve each task-less sibling's
FULL stripped-candidate chain (`full[:n]` for `n` in `5..len(full)`) in
`used`, not just the 5-char placeholder — so a colliding extension is visible
to `_portfolio_prefixes` the same way a real minted prefix is. Alternative:
assign task-less siblings' prefixes in one deterministic global pass instead
of N independent per-project calls, removing the "independently computed"
precondition entirely. Weigh both before implementing — the global-pass
alternative may have a smaller diff and lower risk of a NEW false-collision
regression (CLAWP-096/119/120's whole arc has been about this same function
producing exactly that class of bug when changed).


## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

