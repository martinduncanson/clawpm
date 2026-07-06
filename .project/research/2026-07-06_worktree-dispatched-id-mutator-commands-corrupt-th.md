---
created: '2026-07-06'
id: clawpm-research-worktree-dispatched-id-mutator-commands-corrupt-th
status: open
type: investigation
---
# Worktree-dispatched ID-mutator commands corrupt the main checkout (portfolio-registry bypasses cwd)

## Question

Root cause and workaround for cross-session-reported bug: clawpm tasks state/done/start run from inside a dispatched worktree silently mutate the MAIN checkout instead of the worktree

## Summary

CONFIRMED, root cause identified. `get_project(config, project_id)` in `discovery.py` resolves any ID-based mutator command (`tasks state <id>`, `done`, `start`, `block`) by scanning the GLOBAL portfolio registry (`config.project_roots`) for a directory matching the task-id's project prefix — completely independent of cwd. `create_worktree` (dispatch.py) never registers the created worktree as its own project. So any state-mutator command run from inside a dispatched `--worktree` checkout resolves back to the MAIN checkout's registered `repo_path` and mutates the task file there, regardless of `cd`. This was reported independently by another clawpm-using session (sysops project, two tracks, both self-corrected via git-status diffing) and explains essentially every "stray write to the main checkout" incident hit during the 2026-07-03..06 clawpm dispatch campaign. Filed as CLAWP-098.

## Findings

- The cwd-walk function that exists (`find_project_dir_fallback`) is NOT on the path used by ID-based commands — it's a best-effort fallback for a different resolution mode, not consulted here.
- Since CLAWP-075 tracked `.project/` in git, every worktree checkout carries the identical `repo_path` value in its own `settings.toml` (baked into tracked content) — this makes the bug *more* consequential post-075, not less, contrary to first intuition (one might assume a worktree's own settings.toml would "win").
- "cd into the worktree before running clawpm commands" — guidance given repeatedly to dispatched subagents this campaign — does NOT fix this for ID-based commands. It only helps for commands that resolve project by cwd-walk (a different code path).
- The interim workaround both the external session and this one converged on independently: never run clawpm state-mutator commands from inside a dispatched worktree. Hand-edit the task file's frontmatter and `git mv` it to the target state directory natively — a plain filesystem+git operation with no portfolio-registry involvement, so it stays correctly scoped to the worktree's branch until merge.

## Conclusion

Real, unaddressed, high-priority bug (CLAWP-098, priority 2). Candidate fixes: (a) register the worktree as a temporarily-scoped project for its dispatch lifetime, torn down with `teardown-dispatch`; (b) prefer cwd-resolved `.project/` over the portfolio-registry lookup when cwd is inside a worktree whose id matches the task's project prefix; (c) at minimum, loud warning + documented workaround in dispatch output and SKILL.md until the real fix lands.
