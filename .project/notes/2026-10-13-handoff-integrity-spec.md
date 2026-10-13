# Handoff integrity: close the gaps behind "shared checkouts, unverified handoffs, live loops writing into tracked files"

Date: 2026-10-13. Status: spec, advisor-reviewed. Parent task: see `clawpm tasks list` for the "Handoff integrity" parent.

## Origin and evidence

Complaint quoted by Martin from a Sirvoy remote-VM session: "our real failures: shared checkouts, unverified handoffs and live loops writing into tracked files". The original session text was not retrievable (a GBrain search surfaced only `git-multi-machine-hygiene`, which covers frozen fetch refspecs and cross-machine task-id double allocation). The spec is built from the three phrases plus first-hand evidence from the 2026-10-09..12 clawpm shipping loop:

- A worker wrote into the MAIN checkout twice (a stray `tasks.py` edit and a README), despite `worker-rules.md` forbidding it. The stray edit silently blocked a fast-forward.
- The operator merged with a stale head SHA twice after the orchestrator refreshed a branch ("Head branch was modified"). The orchestrator verified every worker report by hand (`git ls-remote` vs reported head); nothing in clawpm does it.
- The reviewer/worker tooling (`run-codex-gate.ps1`, `worker-rules.md`) lived only in a session scratchpad and had to be copied between sessions by hand.

## Findings from the pre-spec checks

1. **Shared checkouts.** Within one machine clawpm is largely covered (per-project locks CLAWP-051/066/067, session-scoped resolution CLAWP-098, explicit scope CLAWP-122, atomic moves CLAWP-143, id reservations CLAWP-092). Across machines it is NOT: `id_reservations.jsonl` lives under the portfolio root, which is per machine, so two machines on one repo can still mint the same task id. Our earlier answer to Martin overstated coverage for the cross-machine case.
2. **Unverified handoffs.** Not solved. `codex_check.py` / `wait-for-codex` verify a review verdict on a head, and `docs/playbooks/codex-fix-dispatch.md` lists `gh pr view --json mergeable` as a manual success criterion. Nothing verifies that the branch head, the PR head, CI and the worker's reported head agree, and nothing produces the merge command from live data.
3. **Live loops writing tracked files.** `git ls-files .project` shows only durable files tracked (tasks, research, plans, notes). Runtime state (leases, inbox, sessions, iteration log) is not tracked. So there is no runtime-churn problem to split out. The real residue is the loop's booking step (state change plus commit on main) running while the main checkout is dirty or inside a worktree. That is covered by G2, not a separate runtime/durable split.

## Gaps and tasks

All children are kind=build unless stated. Each must ship with tests (TDD), a CHANGELOG line, and README `## All commands` entries for any new command.

### G1 `clawpm pr verify <PR> [--expect-head SHA] [--task ID]` (head attestation)
Resolve the PR through `gh`. Check four facts and report each in JSON (`ok` only if all hold): (a) `git ls-remote fork refs/heads/<branch>` equals `--expect-head` when given; (b) that equals the PR `headRefOid`; (c) CI checks on that exact SHA are all passing; (d) the PR is MERGEABLE. Optionally fold in the review verdict via the existing `codex_check` for the same SHA. Print the exact `gh pr merge N --repo R --squash --match-head-commit <FULL sha>` line built from live data. Never merges. Fail loudly (nonzero exit, structured error) when `gh` is missing, unauthenticated or returns unparseable output; never report a partial pass as ok. Record the verified SHA and verdict on the task when `--task` is given.
Success criteria: stale `--expect-head` is reported as mismatch with both SHAs; a conflicting or pending-CI PR is not ok; missing `gh` is a loud error; the printed merge line carries the full 40-char SHA. Tests mock `gh`/`git` at the subprocess seam.
Out of scope: merging, posting comments, any write to the remote.

### G2 `clawpm tasks land <ID> --pr N` (landing transaction; depends on G1)
After the operator merges: verify the PR is MERGED and its merge commit is an ancestor of `fork/main`. REFUSE (fail closed, nothing changed) when: any tracked path outside the task's own files is dirty in the main checkout (the stray-edit case); the command runs inside a dispatched worktree (CLAWP-098); or the fast-forward is not clean. Then fast-forward main, set the task done, stage ONLY the task's own paths, commit with no attribution trailer. `--push` and `--remove-worktree` are opt-in flags; push is verified with `ls-remote` equal to local HEAD. No destructive recovery of any kind.
Success criteria: dirty unrelated tracked file blocks it with the path named; running from a worktree is refused; success path leaves exactly one new commit touching only the task files; push verification mismatch is a loud error.
Out of scope: merging, force operations, branch deletion.

### G4 Worker write fence (file in the `claude-config` clawpm project, not here)
A harness hook denying a dispatched worker any Write/Edit/Bash write outside its own worktree root. clawpm's part, in this repo: expose the allowed root from the dispatch marker (`clawpm sessions` or the marker file) so the hook has one source of truth. The hook itself and its tests belong in `~/.claude`.

### D1 Decision: cross-machine task-id allocation and state (kind=decision; no build yet)
Same repo on two machines can mint duplicate ids because the reservation ledger is per portfolio root. Decide between: git-ref-based reservation (see CLAWP-061 h5i-style transport), a remote ledger (CLAWP-052 agenticq bridge), or a documented rule (one allocator machine). Output is an ADR note and, if needed, build tasks. Link CLAWP-052 and CLAWP-061.

### P1 Playbook: move loop tooling into the repo
Move `worker-rules.md` and `run-codex-gate.ps1` from the session scratchpad into `docs/playbooks/` (rename: `ship-loop-worker-rules.md`, `run-codex-gate.ps1`), generalise the hard-coded paths, and write `docs/playbooks/ship-loop.md` documenting the orchestrator protocol: round-scoped workers, worktree-per-worker, verify before review (`pr verify`), operator merges with the FULL sha, booking via `tasks land`. Docs only; no src changes.

## Order and parallelism
G1 first. G2 depends on G1 (shared CLI files, so not parallel with it). P1 can run alongside G1 (docs only). D1 is independent. G4 is filed in claude-config and is independent.

## Non-goals
Merging PRs automatically (operator keeps the merge), a runtime/durable `.project` split (checked: not needed), building cross-machine allocation before D1 decides.
