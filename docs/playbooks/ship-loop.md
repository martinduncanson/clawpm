# Ship loop: orchestrator protocol

**Status:** canonical reference for running a multi-task build loop with an orchestrator and short-lived workers.
**Companion files:** `docs/playbooks/ship-loop/worker-rules.md` (what every worker reads first), `docs/playbooks/ship-loop/run-codex-gate.ps1` (the local Codex review gate).
**Related:** `docs/playbooks/codex-fix-dispatch.md` (rubric-dispatched fix iteration), `~/.claude/skills/code-quorum/SKILL.md`.

## The shape

The orchestrator owns the loop and the task state. Workers own one round each. The PR, its review comments and its commits are the baton between rounds. Nothing important lives only in a worker's context or a session scratchpad.

## 1. Round-scoped workers

Brief a worker for ONE round: "implement, push, open the PR, report, stop". Never "until green" and never "drive to merge".

- The worker reads `ship-loop/worker-rules.md`, then its task brief (goal, files, acceptance check, what not to touch).
- The worker stops and reports at about 150k context, or when the round is done.
- On a finding, a failure or a stall, the orchestrator dispatches a FRESH worker from the report. It does not continue the old one: its context is bloated and its framing is stale.
- A stalled worker usually stalled after finishing the edit. Inspect its worktree before discarding anything.

Brief for a failed-round redispatch (self-contained; no patch or test code):

```
Objective:       <the task, one sentence>
Bad pattern:     <what the last worker did wrong or repeated>
Known evidence:  <findings, failing test names, exact sha, PR number>
Next step:       <the one thing to do this round>
Recovery signal: <the observable result that shows the round worked>
```

Restart only when the worker is not converging (repeats a mistake after steering, reintroduces a fixed defect, ignores findings). A round count alone is not a reason.

## 2. Worktree per worker

Each worker gets its own worktree off `fork/main`:

```
git -C <repo> fetch fork
git -C <repo> worktree add <repo>/.clawpm-worktrees/<ID> -b clawpm/<ID> fork/main
```

Pass `fork/main` explicitly. Parallel workers must have disjoint file sets; the shared exception is `CHANGELOG.md`, where a merge conflict is expected and resolved by keeping both lines. Never edit the main checkout from a worker.

## 3. Commit every round

Commit at the end of every round, including partial work, and push. An unpushed commit is not a baton. Do not force-push or rebase a pushed branch; integrate main with `git merge fork/main`.

## 4. Verify before review

Before any review runs, check that what the reviewer will read is what the worker claims:

```
git -C <wt> rev-parse HEAD
git ls-remote fork refs/heads/clawpm/<ID>
```

The two shas must match, in full. A mismatch means the push failed or lagged; reviewing then reviews stale code. A worker report that quotes an abbreviated sha, or no sha, is not verified.

`clawpm pr verify <PR> --expect-head <sha>` (CLAWP-147-001) performs this check in one command and prints the exact merge line. The manual check above is the fallback.

## 5. Codex gate

Run the local gate on the pushed head:

```
pwsh -NoProfile -File docs/playbooks/ship-loop/run-codex-gate.ps1 `
    -Worktree <wt> -Base fork/main -Brief <brief.md> -Out <out.txt> -ReviewId <id>
```

Use a per-session `-Brief` and `-Out` path. The shared temp-dir `rev-brief.md` is machine-global and concurrent sessions overwrite it. The script nonces the verdict line, parses it with the code-quorum helpers and logs to the find-log. Exit 2 or `CODEX_GATE: ERROR` is a gate failure, not a clean result. If the PR-side `@codex` review is in use instead, only a top-level `@codex` PR comment triggers it; silence after a push is a missing trigger, not a clean review.

### Stop rule

- A round with only P3 findings: merge. Log them; do not start another round.
- Sort every finding as required behaviour, discretionary refinement or cosmetic preference. Only required behaviour blocks the merge. Log the rest.
- Ask the reviewer for every defect in one pass, then a completeness check: "if the worker fixed exactly these defects and changed nothing else, would you accept?" If not, the list is incomplete. This targets findings that drip in one per round.
- A recurring bug class across rounds: stop fixing instances. Audit the whole class with an exhaustive search and fix it at the source, in one worker round.
- Findings per round not falling: attribute them by source. One feature generating findings every round belongs in its own PR.
- A fix must not turn a loud failure into a silent fall-through. Check this before accepting it.

## 6. Operator merge

The operator merges; workers and the orchestrator never do. Merge against the exact reviewed commit, using the FULL 40-char head sha:

```
gh pr merge <N> --repo <owner>/<repo> --squash --match-head-commit <40-char-sha>
```

Take the sha from `gh pr view <N> --json headRefOid`. An abbreviated sha fails GraphQL coercion and the PR stays open. `--match-head-commit` refuses the merge if the branch moved after review.

If `gh pr checks` reports nothing after a push, check `mergeable` first: a sibling merge usually made the PR conflicting. Merge `fork/main` into the branch and push.

## 7. Booking

Task state changes only from the MAIN checkout, never from inside a worktree (CLAWP-098):

```
clawpm tasks state <ID> done
```

There is no `tasks done` subcommand. Before booking, run `git status` in the main checkout and read the full fast-forward output: a worker script can write files into the main checkout and silently block the ff-merge.

`clawpm tasks land <id> --pr <N>` folds the merge check and the booking into one command. It never fetches: run `git fetch <remote>` first, or it refuses `remote_not_fetched`. The task file must be clean in the working tree and identical to the tip's copy, or it refuses before changing anything. Until the command is on your PATH, book by hand.

## Checklist for the orchestrator

1. Brief names ONE round, the task spec, the acceptance check, files in scope.
2. Worker pushes; `ls-remote` sha equals local HEAD (full sha).
3. Gate runs on that sha; apply the stop rule.
4. Operator merges with `--match-head-commit <full sha>`.
5. Book from the main checkout; add the CHANGELOG line in the same PR.
