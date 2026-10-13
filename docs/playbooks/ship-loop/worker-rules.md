# Worker rules (ship loop) - READ FIRST, then your task brief

Repo: the main checkout of the project (branch `main`, tracking `fork/main`). Remote `fork` is canonical. `origin` is upstream: never push there. Substitute `<repo>` below with the main checkout path and `<wt-root>` with `<repo>/.clawpm-worktrees`.

You work ONLY in your own git worktree. Never edit files in the main checkout. Never run `clawpm tasks` mutators from inside a worktree (CLAWP-098: ID-mutator commands resolve through the global portfolio registry, not the cwd). You do not change task state at all; the orchestrator does, from the main checkout.

Scope of ONE round: do the brief, push, report, STOP. Do not merge PRs (the operator merges), do not loop "until green", do not start extra work. Stop and report by about 150k context.

## Mechanics

- Use absolute paths or `git -C <worktree>`; a bare `cd` moves the session working dir.
- New worktree (only if the brief says create one): `git -C <repo> fetch fork`, then `git -C <repo> worktree add <wt-root>/<ID> -b clawpm/<ID> fork/main`. Always pass `fork/main` explicitly: the default base can resolve to `origin` (upstream) and land on ancient history.
- Python / clawpm / pytest on Windows: use the PowerShell tool with `$env:PYTHONIOENCODING='utf-8'` (the Bash tool exits 127 on `.py`). Run pytest from the worktree root. NEVER `pip install -e` from a worktree: it repoints the global install and breaks it when the worktree is removed.
- Run the FULL suite in the FOREGROUND (about 4-5 min; set a long tool timeout). Do not background it: workers that background their suite go idle and never wake. Do not chain two full suites in one command.
- TDD: write the failing test first, see it fail for the stated reason, then fix. Match surrounding code style and comment density. Touch only what the task needs. Python 3.11 compatible, Windows + CRLF safe (`Path.write_text` is TEXT mode; use `newline=''` or `write_bytes` where byte identity matters).
- Every merged feature or fix adds ONE line under `## [Unreleased]` in `CHANGELOG.md` (Added / Changed / Fixed, cite `(#PR, TASK-ID)`). A new CLI option or command must appear in README `## All commands` (staleness gate test).
- Commits: no attribution trailers anywhere (commits, PR bodies). Plain messages like `fix(CLAWP-NNN): ...`. Avoid backticks inside double-quoted Bash strings; write commit and PR bodies to a real file and use `git commit -F file` / `gh pr create --body-file file`.
- Commit every round, even partial work. The branch and its commits are the baton the next worker picks up.
- Push: `git -C <wt> push fork clawpm/<ID>`. No force-push, no rebase of pushed branches; to integrate main use `git merge fork/main`. GitHub push 500s are intermittent: retry after about 90 s. VERIFY with `git ls-remote fork refs/heads/clawpm/<ID>` equal to `git rev-parse HEAD`, and quote the FULL 40-char sha.
- PR: `gh pr create --repo <owner>/<repo> --base main --head clawpm/<ID> --title ... --body-file <file>`.
- Before reporting: `git status` clean, pushed, suite result (count passed / failed).

## Report format (final message, under 25 lines)

Branch + FULL head sha (ls-remote verified), PR number and url, suite result, what changed (files), tests added, doubts and residual risks, anything you chose not to do and why. Facts only.
