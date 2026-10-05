---
baseline_ref: f3a9e3c
created: '2026-10-05'
id: CLAWP-134
predictions:
  complexity: s
  confidence: 4
  duration_min: 120
  filled_by: agent
  pre_mortem: git check-ignore behaves differently for a project in a repo subdirectory
    or a worktree
  success_criteria:
  - clawpm doctor WARNs with the .gitignore file:line when .project/ task files are
    ignored; silent when only the lock is ignored or opt-out set
priority: 3
updated: '2026-10-05'
---
# doctor + init warn when a project's .project/ task state is gitignored or untracked

## Problem

clawpm's whole value is persistent task state. Nothing in clawpm notices when that state isn't versioned. On 2026-10-05, a portfolio scan of `F:/Git/*/.gitignore` found two repos ignoring all of `.project/`:

- `polymarket-arb`: `.gitignore:28` is `.project/`. It was added 2026-04-26 in an unrelated commit (118782f) with no rationale. 120 task files plus SPEC, learnings and settings (343K) have never been on any git ref. The repo is private.
- `sirvoy-business-data-warehouse`: `.gitignore:22` is `.project/`, with 2 tasks. This is a client repo, so any fix there is an operator call.

Every other clawpm repo ignores only `.project/tasks/.clawpm-tasks.lock`, which is correct. clawpm itself had the same bug until CLAWP-075.

## Proposed fix (deterministic, no model)

1. `clawpm doctor` adds a check: for each project, run `git check-ignore -q <project>/.project/tasks` (or check `git ls-files` is non-empty for `.project/tasks/*.md`). WARN when task files exist but are ignored or untracked. Show the matching `.gitignore` line (`git check-ignore -v`) and the fix: replace `.project/` with `.project/tasks/.clawpm-tasks.lock`.
2. `clawpm init` / project creation: if the repo's `.gitignore` would ignore `.project/`, print the same warning at creation time.
3. Optional: an opt-out per project (`settings.toml` `unversioned_ok = true`) for repos that deliberately keep PM state off git, so doctor stays quiet there.

Not in scope: auto-editing `.gitignore` or committing in a user's repo. That's a repo-owner action.

## Acceptance criteria

- A test where `.gitignore` contains `.project/` and task files exist: `clawpm doctor` reports a WARN naming the ignore rule's file:line.
- A test where only the lock file is ignored: no warning.
- A test where the opt-out is set: no warning.
- Full suite passes.


## Notes

