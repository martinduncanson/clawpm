# Dispatch fan-out pattern

**Status:** canonical reference for converting a single dispatched task into an in-session
parallel decompose→verify→converge workflow, using the **harness's native fan-out primitive**
(no subprocess, no extra dependency).
**Primitives:** CLAWP-016 (rubric), CLAWP-017 (Stop-hook condition evaluator), CLAWP-018
(subagent dispatch) + the agent harness's built-in workflow/fan-out tool.
**Sibling playbook:** `docs/playbooks/codex-fix-dispatch.md` (iteration-loop dispatch).
**Companion spec:** `../WORKFLOW-RUNTIME-INTEGRATION.md` (the *out-of-harness* path — only when
there's no fan-out-capable harness in the loop).

## The distinction this playbook turns on

clawpm `tasks dispatch` instruments a directory with hooks; the **worker runs in an agent
harness**. If that harness already has a fan-out primitive (Claude Code's `Workflow` tool, or
any equivalent parallel/pipeline orchestrator), a dispatched worker can fan out **natively** —
no clawpm code change, no Node runtime, no subprocess bridge. This playbook is the authoring
guidance for that case. It is the **default** answer for fan-out-shaped dispatched tasks.

Reach for the out-of-harness runtime (`../WORKFLOW-RUNTIME-INTEGRATION.md`) **only** when there is
no fan-out-capable harness: local-model dispatch, headless cron/CI, a non-Claude harness without
the primitive. Don't build subprocess plumbing to get a capability the worker already has.

## When to use this pattern

The dispatched task is **fan-out-shaped** — it decomposes into many independent sub-decisions
that can run in parallel and be verified independently:

- "Review N files / modules and verify each finding" (the canonical shape).
- "Research a question across M sources, then cross-check the claims."
- "Apply the same transform to K sites and verify each independently."
- Any task where the work is *unbounded discovery* (find all the X) rather than a *fixed
  sequence* of steps.

If the task is a linear sequence with operator decisions per step, this is not the pattern —
dispatch it normally, or leave it in the direct-work flow.

## The three authoring patterns (port these, not the plumbing)

These are the high-value patterns demonstrated in the workflow-runtime examples
(`F:/git/workflows/examples/`). They are **harness-agnostic authoring shapes** — express them
with whatever fan-out tool the worker's harness provides.

### 1. Decompose → flat schema → parallel barrier → early-exit

Break an open-ended task into single yes/no decisions, one agent per `(unit × category)`. Each
agent fills a **flat** structured-output schema (a weak/local model can do this reliably; a
nested schema is where small models fail). Fan out with a barrier, filter, and **early-exit**
if nothing was found — don't run the verify stage on an empty set.

Reference: `F:/git/workflows/examples/small-model-review.workflow.js`.

### 2. Adversarial / lens-varied multi-vote verification

A finding is **not trusted because one agent reported it.** Other agents are tasked with
**refuting** it. Critically, vary the *lens* per voter (correctness / edge-cases / reproduction)
so the votes are genuinely independent — K identical prompts collapse to one cached result and
give false confidence. Compute the survival threshold over the **spawned** vote count (a null/
abstention counts against, doesn't vanish), with a **default-to-not-real** bias. Only findings
that survive the refutation pass reach the operator.

> If a subagent claims function F has a race condition, another subagent's job is to **disprove
> it**. Survivors only.

### 3. Convergence-driven iteration (dry-round counter)

For genuinely unbounded discovery, keep spawning finder rounds until **K consecutive rounds
surface nothing new** (a "dry-round" counter), deduping each round against everything seen so
far. The number of agents and iterations is decided **at runtime by what the work surfaces** —
not a fixed step count. Dedup against the *seen* set, not the *confirmed* set, or
judge-rejected findings reappear every round and the loop never converges.

Reference: `F:/git/workflows/examples/plan-verify-converge.workflow.js`.

**Scope note:** convergence-iteration fits *open-ended discovery* tasks. It does **not** fit
fixed-rubric grading (where the criteria are known and enumerable — there's nothing to converge
over). For the rubric gate, see the adversarial note in the cost/judge section below.

## How it composes with the rubric gate

The dispatched worker fans out internally (patterns 1–3); the clawpm **Stop hook still owns the
terminal gate.** The worker cannot exit until `evaluate_stop_condition` grades the task's rubric
satisfied. So the contract is unchanged — fan-out is *how the worker does the work*, the rubric
is *whether the work is done.* Write the rubric so its `gradeable_signal` can be satisfied by the
worker's fan-out output (e.g. "confirmed findings written to `<path>` as JSON, each with ≥2
surviving refutation votes").

## Dispatch invocation

Identical to a normal CLAWP-018 dispatch — the fan-out is in the worker's *mandate*, not in
clawpm:

```bash
# 1. File the fan-out task with a rubric whose signal the fan-out output satisfies.
clawpm tasks add --project <proj> \
    -t "Fan-out review: <scope>" \
    --predict-duration 45m --predict-complexity m --confidence 3 --predicted-by agent \
    --success-criteria "Every file in <scope> reviewed; each confirmed finding survives >=2 lens-varied refutation votes; results written to <out>.json" \
    --success-criteria "Zero unverified findings in <out>.json (every entry has votesReal>=ceil(votesCast/2))"

# 2. Render the rubric to confirm it's gradeable.
clawpm tasks emit-rubric <CLAWP-id> --rubric-format markdown

# 3. Dispatch (worktree for parallel-safety).
clawpm tasks dispatch <CLAWP-id> --worktree
```

Then launch the worker against the worktree with a mandate that names the fan-out shape:

```
Task tool (or operator) prompt:
  "You are in <repo>/.clawpm-worktrees/<CLAWP-id>/. The task is fan-out-shaped.
   Use your harness's workflow/fan-out tool: decompose <scope> into one
   decision per (file x category) with a flat schema, fan out in parallel,
   then run lens-varied multi-vote refutation on every finding (default to
   not-real; survivor threshold over spawned votes). Write confirmed findings
   to <out>.json. The Stop hook grades the rubric — you cannot exit until it
   passes."
```

Teardown is the standard `clawpm tasks teardown-dispatch <CLAWP-id>` on completion.

## Known limitations

1. **The harness must actually have a fan-out primitive.** If the worker runs in a harness
   without one (local model, bare CLI), this playbook doesn't apply — that's the
   `../WORKFLOW-RUNTIME-INTEGRATION.md` case. Confirm the harness capability before choosing this
   pattern.
2. **Fan-out cost is real.** Multi-vote refutation multiplies model calls per finding. On a
   subscription this is rate-limit pressure; against an API key it's money. Bound the fan width
   and vote count in the worker's mandate.
3. **Dedup discipline is load-bearing.** Convergence iteration that dedups against the wrong set
   (confirmed instead of seen) never terminates. State the dedup key explicitly in the mandate.

## Cross-references

- `F:/git/workflows/examples/small-model-review.workflow.js` — patterns 1 + 2 end to end.
- `F:/git/workflows/examples/plan-verify-converge.workflow.js` — pattern 3 (dry-round convergence).
- `F:/git/workflows/docs/layer-b-authoring-guidance.md` — the authoring doctrine these shapes come from.
- `src/clawpm/judges/stop_condition.py` — the rubric gate the fan-out runs underneath.
- `docs/playbooks/codex-fix-dispatch.md` — the iteration-loop sibling pattern.
- `../WORKFLOW-RUNTIME-INTEGRATION.md` — the out-of-harness path, for when no fan-out primitive exists.
