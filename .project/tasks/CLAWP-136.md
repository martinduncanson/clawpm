---
baseline_ref: 34facb4
created: '2026-10-06'
id: CLAWP-136
predictions:
  approach: resolve_path_or_none at the dispatch site; then scope= on tasks.py mutators
    + is_task_store_canonical; migrate call sites one by one
  complexity: m
  duration_min: 120
  filled_by: agent
priority: 5
updated: '2026-10-06'
---
# CLAWP-122 follow-ups: guard dispatch --target-dir resolve; migrate hand-scoped sites to explicit Scope

Follow-up from CLAWP-122 (PR #75, merged 34facb4, 2026-10-06).

1. `Path(target_dir).resolve()` at `src/clawpm/cli/tasks.py:~1387` (`tasks dispatch --target-dir`) has no OSError guard. An unavailable target crashes the command; ambient resolution logs and falls back to canonical. Same class as PR #75's Codex r1 P2 #2. Fix: route it through `sessions.resolve_path_or_none` (added in #75).

2. Migrate the remaining hand-scoped call sites to explicit `Scope` (list in `docs/design/explicit-scope.md`): agent.py dispatch_agent, leases.apply_fallback, cli/lease.py, cli/project.py doctor --apply, cli/tasks.py dispatch --target-dir. Prerequisite: `scope=` on the tasks.py state mutators (add_task, change_task_state, list_tasks, ...) and on `is_task_store_canonical`.

## Success criteria
- A test where `tasks dispatch --target-dir <path whose resolve() raises OSError>` logs a warning and falls back to canonical instead of raising (RED before, GREEN after).
- Each migrated call site resolves scope once at its command entry point; existing worktree-scope tests still pass, and the full suite is green.


## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

