# Codex-fix dispatch pattern

**Status:** canonical reference for converting Codex review-iteration loops to rubric-dispatched subagents.
**Primitives:** CLAWP-016 (rubric), CLAWP-017 (Stop-hook condition evaluator), CLAWP-018 (subagent dispatch).
**Skill cross-ref:** `~/.claude/skills/codex-review/SKILL.md` — workflow steps 6 (BRIEF) through 9 (ITERATE).

## When to use this pattern

Convert any work unit that:

1. Has a known **machine-verifiable terminal state** (Codex returns clean, tests pass, encoding scan empty).
2. Involves **repeating cycles** of "fix → push → wait → re-review → fix" — the manual iteration loop the codex-review skill describes.
3. Doesn't require operator judgment on each cycle (it's iteration, not design).

If the cycle needs operator design decisions per round, leave it in the direct-work flow — dispatch is not the answer.

## Why it beats direct iteration

| Direct flow (current) | Dispatched flow (this pattern) |
|---|---|
| Parent agent holds context across all rounds (~3-5 rounds × 5-15min each = 15-75min of context burn) | Subagent owns the loop; parent gets a single Stop notification + verdict |
| Parent must remember to wait, re-ping, triage findings | Stop-hook auto-terminates when rubric is satisfied; parent picks up the result |
| Each round costs the full system prompt + accumulated round history in subagent attention | Subagent loop runs with bounded context; rubric is the contract |
| Operator must trust the parent agent's "looks clean to me" call | Judge independently grades against criteria; no self-verification |

## The canonical rubric

For a Codex-fix iteration loop on PR #N in repo `<owner>/<repo>`, the rubric criteria are:

```
1. Codex returns clean (no major issues) on the PR head commit after the LAST push
   gradeable_signal: scripts/wait-for-codex.py exits with code 3 (the "👍 / Breezy! / Chef's kiss" signal),
                     OR returns a review/comment body matching /no major issues|chef.s kiss|breezy|looks good/i
   comparator: signal_present == true

2. The local test suite passes against the PR head
   gradeable_signal: `python -m pytest -x -q` exit code
   comparator: exit_code == 0

3. The PR is still in MERGEABLE state on GitHub (rebased / conflicts resolved if any landed)
   gradeable_signal: `gh pr view <N> --json mergeable -q '.mergeable'`
   comparator: value == "MERGEABLE"
```

These three together form the strict "PR is ready to merge" contract. The judge fires at every Stop event during the dispatched subagent's lifecycle; the subagent literally cannot exit until all three pass.

## The dispatch invocation

### 1. File the iteration task with the structured rubric

```bash
clawpm tasks add --project <proj> \
    -t "Codex-fix iteration loop: PR#<N> <short-title>" \
    --priority 3 \
    --predict-duration 1h --predict-complexity m --confidence 3 \
    --predict-approach "Run codex-review skill workflow: triage Codex findings on the latest review, fix, commit, push, re-ping @codex, wait. Loop until rubric satisfied." \
    --predicted-by agent \
    --reference-task <prior-similar-CLAWP-id> \
    --success-criteria "Codex returns clean on PR#<N> head: wait-for-codex.py exit==3 OR review body matches /no major issues|breezy|chef.s kiss|looks good/i" \
    --success-criteria "Local test suite passes against PR head: pytest -x -q exit_code==0" \
    --success-criteria "PR#<N> mergeable: gh pr view <N> --json mergeable -q '.mergeable' == MERGEABLE"
```

### 2. Verify the rubric is renderable

```bash
clawpm tasks emit-rubric <CLAWP-id> --rubric-format markdown
```

If criteria are too vague to grade, the rubric will read as unfollowable. Tighten before dispatching.

### 3. Dispatch via worktree (parallel-safe)

```bash
clawpm tasks dispatch <CLAWP-id> --worktree
```

This:
- Creates `<repo>/.clawpm-worktrees/<CLAWP-id>/` (isolated git worktree on a fresh branch off `main`).
- Writes `<worktree>/.claude/settings.local.json` with the hook configuration from CLAWP-018:
  - **PostToolUse** matcher → logs each Bash invocation to the iteration log.
  - **Stop** matcher → evaluates the rubric via `clawpm.judges.stop_condition.evaluate_stop_condition`. Blocks termination if `verdict.ok == false` and prompts the subagent with the specific failing criterion + gradeable signal.
  - **SessionStart** matcher (unless `--no-session-context`) → injects the rendered rubric at session start so the subagent knows the contract upfront.
- Marks the task as `in_progress` in clawpm; the subagent is the owner from this point.

### 4. Spawn the subagent against the worktree

From the parent agent's perspective (or via Claude Code's Task tool), launch the subagent with the worktree as its working directory and the codex-review skill workflow as its mandate:

```
Task tool invocation:
  description: "Codex-fix loop PR#<N>"
  prompt: "You are in <repo>/.clawpm-worktrees/<CLAWP-id>/.
           Follow the codex-review skill workflow (steps 6-9) to iterate
           until the Stop-hook rubric passes. Do not attempt to terminate
           until all three criteria are satisfied. The judge will tell
           you which criteria are failing on each Stop event."
```

The subagent runs autonomously. Parent gets notified when the rubric finally passes and Stop unblocks.

### 5. Teardown

```bash
# After verdict.ok == true and subagent terminates:
clawpm tasks teardown-dispatch <CLAWP-id>
# Removes the worktree, cleans up settings.local.json, marks task done.
```

## Known limitations

1. **Codex latency vs Stop-hook polling.** Codex can take 5-15 minutes between push and review. The dispatched subagent must use `scripts/wait-for-codex.py` as a blocking call between iterations (it polls 3 surfaces and supports re-pinging). It must NOT poll inside the agent's turn (would burn context).
2. **Network-async grading.** The Codex-clean signal lives on GitHub, not in the local repo. The gradeable_signal for criterion 1 invokes `wait-for-codex.py` which is itself a polling wrapper — the judge effectively delegates to it. Acceptable but worth noting.
3. **No worked example yet.** This pattern is documented but not yet executed end-to-end against a live PR. First conversion should happen on the next naturally-occurring Codex iteration loop (likely arising from the upstream PR backlog or a new feature PR).
4. **Per-call exit code on chained iteration.** If the subagent runs multiple iterations within one Bash event chain, the PostToolUse hook captures one exit code for the whole chain (per the round-2 Codex fix on `hooks/clawpm-sync/handler.py`). Iteration boundaries are still discernible from the rubric verdict log, but per-step exit-code granularity is lost.

## When this pattern fails (pre-mortem)

- **Failure: subagent terminates prematurely.** Cause: rubric criterion 1 written too loosely — e.g. `"Codex commented"` without checking for the clean signal vs findings comment. Mitigation: always require `wait-for-codex.py` exit==3, never just "Codex replied".
- **Failure: subagent loops indefinitely on a finding it doesn't understand.** Cause: real Codex finding requires operator design decision. Mitigation: dispatch task should include a `--predict-pitfalls` enumerating known-ambiguous code areas; subagent escalates via `clawpm tasks state <id> blocked` if it hits one of those.
- **Failure: Codex environment removed mid-iteration.** Cause: chatgpt.com/codex/cloud env de-provisioned. Mitigation: subagent detects "To use Codex here, create an environment" reply, marks blocked, exits gracefully.

## Cross-references

- `~/.claude/skills/codex-review/SKILL.md` — the manual workflow this pattern automates.
- `~/.claude/skills/codex-review/scripts/wait-for-codex.py` — 3-surface poll, the gradeable signal source.
- `src/clawpm/judges/stop_condition.py` — `evaluate_stop_condition` implementation.
- `src/clawpm/dispatch.py` — settings.local.json + worktree management.
- CLAUDE.md "Task definition discipline" section — the upstream policy this pattern operationalises.
