# Explicit scope at the command entry point (CLAWP-122)

## Problem

Worktree scoping (CLAWP-098) is implicit. `get_project_dir` and friends decide
"canonical checkout or this worktree?" from ambient state: the process cwd plus
the `suppress_session_resolution()` contextvar. A new command author gets no
signal that scope is a decision at all. PR #55 rounds 13-17 found five commands
(`dispatch_agent`, `log add/commit`, `leases.apply_fallback`, `doctor --apply`,
`lease grant`) that each needed a hand-made scope fix, one review round apiece.

## API

`sessions.Scope` is a small frozen value with two modes:

- `Scope.canonical()` never redirects to a worktree. Same answer as inside
  `suppress_session_resolution()`.
- `Scope.bound(path)` resolves sessions as if `path` were the cwd. Same answer
  as inside `resolve_scope_from(path)`.

Neither mode reads the cwd or the contextvars. A bound scope stays put if the
cwd changes mid-command.

`Scope.pinned(project_dir)` (CLAWP-115) is a third, session-free mode: the task
store IS that `.project/` directory. `get_project_dir` returns it verbatim, with
no session lookup and no registry fallback. Only the project dir (so the tasks
dir) is pinned; repo path and settings stay canonical. `change_task_state` and
`parent_rollup_status` accept `scope=` and thread it to every store lookup. The
agent-dispatch verdict sync uses it to touch only the worktree's own store.

`discovery.resolve_scope(config, project_id, target_dir=None)` builds one. With
`target_dir` it binds to that directory. Without it, it freezes what ambient
resolution would answer now: canonical inside `suppress_session_resolution()`,
else bound to the current cwd (or the `resolve_scope_from` override).

These accept a keyword-only `scope: Scope | None = None`:

- `discovery.get_project_dir`, `get_repo_path`, `get_scoped_project_settings`
- `tasks.get_tasks_dir`, `get_task`, `touch_task_updated`

`scope=None` is exactly the old ambient behaviour. A non-None scope bypasses
cwd and contextvar resolution entirely, including `suppress_session_resolution()`.

## Decision: opt-in (2026-10-06)

The task asked whether to migrate every call site now or make the scope
parameter opt-in. Decision: opt-in. Existing callers are unchanged and behave
identically. Callers migrate opportunistically. Reason: backward compatibility,
and the operator prefers compat over churn. A bulk migration touches
auth-adjacent write paths for no behaviour change.

## Using it in a new command

1. Resolve once, at the top of the command: `scope = resolve_scope(config, project_id)`.
   For a command that runs somewhere else (`--target-dir`), pass `target_dir=`.
   For portfolio-wide housekeeping on tasks the operator did not name, use
   `Scope.canonical()`.
2. Pass `scope=scope` to every scoped lookup the command makes. Do not call
   the ambient form for one lookup and the scoped form for another: mixing them
   is the bug class this exists to remove.
3. Never call `resolve_scope` twice in one command. The point is one decision.

`log add` (`src/clawpm/cli/log.py`) is the worked example. It binds once, then
threads the scope into `get_repo_path` and `touch_task_updated`.
`tests/test_explicit_scope.py` pins that it resolves exactly once and stays bound
when the cwd moves mid-command.

## Migration guidance: remaining hand-scoped sites

Still on ambient resolution plus a hand-placed contextvar:

- `agent.py` (`dispatch_agent`): `suppress_session_resolution()` around the
  nested worktree. Maps to `Scope.canonical()`.
- `leases.py` (`apply_fallback`): sweep under `suppress_session_resolution()`.
  Maps to `Scope.canonical()`, but needs `scope=` threaded through the
  task-state functions it calls first.
- `cli/lease.py` (`lease grant`): `is_task_store_canonical` check plus
  `suppress_session_resolution()`.
- `cli/project.py` (`doctor --apply`): `suppress_session_resolution()` around
  the lease sweep.
- `cli/tasks.py` (`tasks dispatch --target-dir`): `resolve_scope_from(target)`.
  Maps to `resolve_scope(config, pid, target_dir=target)`. The `lease_ttl`
  guard uses `is_task_store_canonical`, which has no `scope=` yet.

Most blocked on one thing: the task-state mutators in `tasks.py` (`add_task`,
`change_task_state`, `list_tasks`, and others) still call `get_tasks_dir`
without a scope. Thread `scope=` through them before converting the sites above.
`is_task_store_canonical` should gain the same keyword.
