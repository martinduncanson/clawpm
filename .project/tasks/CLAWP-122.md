---
baseline_ref: a5cd2e4
created: '2026-09-22'
id: CLAWP-122
predictions:
  complexity: l
  duration_min: 240
  filled_by: agent
  hypothesis: 'If session scope is resolved once at each command entry point and threaded
    explicitly instead of re-derived ambiently per-call, future commands stop needing
    individually-discovered scope fixes (5 found across PR #55 rounds 13-17)'
priority: 4
tags:
- concurrency
- architecture
updated: '2026-09-22'
---
# Explicit scope-at-entry-point refactor (replace implicit worktree redirect)

## Problem

CLAWP-098 (PR #55, rounds 13-17) fixed worktree-scope leaks one command at a
time as Codex/local reviewers found new ones: `dispatch_agent`, `log add`/
`log commit`, `leases.apply_fallback`'s canonical guard, `doctor --apply`.
Each fix follows the same shape — a command mixes a canonical
(cwd-independent) lookup with a session-scoped (cwd-redirected) one, or
forgets to suppress/scope the redirect for portfolio-wide housekeeping that
processes a task the operator didn't name.

The root cause: the worktree redirect lives in the GLOBAL `get_project_dir`
chokepoint (`discovery.py`), applying implicitly to every caller based on
ambient cwd. A new command author has no signal that they need to think
about scope at all — the redirect just silently does (or doesn't) apply,
and the only way to find out is to hit it with a reviewer or an incident.

## Proposal

Replace the implicit global redirect with an EXPLICIT scope resolved once at
each command's entry point and threaded down, instead of every function in
the call graph independently consulting ambient cwd/contextvars. Something
like:

```python
scope = resolve_dispatch_scope(config, project_id)  # from cwd, or an
                                                       # explicit override
# ... pass `scope` down explicitly to get_tasks_dir/get_repo_path/etc,
# instead of them re-deriving it from cwd/contextvars each call.
```

This makes "did I bind this command to a scope?" a visible, reviewable
decision at the point a new command is written, rather than an implicit
property of whatever get_project_dir happens to do today.

## Motivating evidence (PR #55 rounds 13-17)

- Round 13: `add_task`'s prefix resolution read the canonical settings.toml
  while the task store itself was worktree-scoped.
- Round 14: `dispatch_agent`'s nested worktree needed FORCED canonical
  resolution (`suppress_session_resolution`); `tasks dispatch --target-dir`
  needed the OPPOSITE — resolution bound to the target, not the caller.
- Round 15: `--target-dir` inside a DIFFERENT registered worktree needed
  `resolve_scope_from(target)` — ambient cwd was never the right signal at
  all. `log add`/`log commit` (the PostToolUse progress hook) needed the
  session-scoped `get_repo_path`, discovered independently of the dispatch
  path.
- Round 16: `leases.apply_fallback` (deliberately canonical, round 3) and
  `doctor --apply` (accidentally NOT canonical) both needed explicit
  scope decisions that the implicit redirect didn't surface.
- Round 17: `lease grant` needed a canonical-EXISTENCE check distinct from
  the ambient-scope check, because it never loads the task at all — the
  two guards test different things and neither alone is sufficient.

Five commands, five rounds, each independently discovering the same design
gap. That is the signal this task exists to close — not by finding a sixth
command, but by removing the implicit-redirect footgun structurally.

## Success criteria
- Design note (or code) proposing the explicit-scope-at-entry-point API.
- At minimum, `get_project_dir`/`get_tasks_dir`/`get_repo_path` accept an
  optional explicit scope parameter that bypasses ambient cwd/contextvar
  resolution when provided, with existing callers unchanged (backward
  compatible) and new/refactored callers passing it explicitly.
- A decision (with the operator) on whether to migrate ALL existing call
  sites now, or make explicit-scope opt-in and migrate opportunistically.

## Out of scope
Fixing any NEW worktree-scope leak discovered by future review rounds —
patch those narrowly with the existing pattern; this task is about removing
the need to keep finding them.


## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

