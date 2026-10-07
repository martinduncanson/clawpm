# CLAWP-070: `clawpm loop` evaluation

Decision date: 2026-10-07. Status: decided, shipped.

## Decision

**Fold the missing knob into dispatch. Do not build a `clawpm loop` command.**

The knob is `clawpm tasks dispatch --max-iterations N`: an absolute cap on
Stop-hook rubric iterations. `--max-budget` is deferred (see below).

## What already exists

| Need from the spec | Existing primitive |
|---|---|
| Keep working until the rubric is satisfied | `tasks dispatch` wires a Stop hook (`hook eval-stop`) that blocks the subagent from stopping until the judge says `ok` (CLAWP-017/018). |
| Detect a stuck loop | `detect_thrashing` (CLAWP-062): N consecutive not-ok, non-impossible verdicts trips a stop. Threshold is per-task, env, or default 4. |
| Per-iteration log | `write_iteration_event` writes one `iteration_event` per Stop-hook cycle (CLAWP-019). `count_iterations_for_task` reads it. |
| Operator-facing "keep going" | The loop already runs inside one subagent session. The operator does not re-invoke dispatch each round. |

## Why a new command is redundant

The task text assumes the operator re-invokes dispatch each round. They do
not. The iterate-grade-revise loop lives in the Stop hook. A `clawpm loop`
command would be an outer loop that spawns a fresh session per round. That
costs context (each round restarts cold), duplicates the judge call the hook
already makes, and needs its own subprocess-launch and transcript plumbing
that clawpm does not have. It would be a thin wrapper around machinery that
already terminates on rubric-satisfied.

The spec's own pre-mortem said: if it is just a thin wrapper, fold the
missing knob into dispatch.

## What was actually missing

Termination today: rubric satisfied (`ok`), `impossible`, or thrashing.
Thrashing only catches runs where every one of the last N verdicts is
not-ok. It does not bound a run that makes slow, real progress, and it does
not bound total spend. So the real gap is an **absolute iteration cap**.

## What shipped

- `tasks dispatch --max-iterations N` (N >= 1). Optional; default uncapped.
- The Stop-hook command carries `--max-iterations N --iteration-baseline B`.
  `B` is the iteration count already on record at dispatch time, so a
  re-dispatch after a capped run gets a fresh budget instead of tripping on
  its first verdict.
- `hook eval-stop` converts a would-be block into a
  `stop_condition_tripped` verdict with a `MAX_ITERATIONS` message once
  `count - baseline >= N`. It reuses the CLAWP-062 trip path, so the operator
  sees the same "stopped, triage" surface as thrashing.
- An `ok` or `impossible` verdict is never converted. A rubric satisfied on
  the cap iteration still closes as satisfied.
- If the cap cannot be evaluated (I/O error reading the log) the hook fails
  open and writes a warning to stderr. Wedging the agent would be worse.
- Per-iteration progress log: unchanged. The existing iteration events are it.

## Deferred: `--max-budget`

The iteration log records no token or cost data, and the Stop hook does not
receive any. A budget cap would need a trustworthy spend signal first.
Wall-clock minutes could be derived from the dispatch marker's
`dispatched_at`, but wall-clock is a poor proxy for cost and was not asked
for. Follow-up if wanted: record per-iteration timestamps-and-usage in the
iteration event, then cap on that. Not built here.

## Out of scope

Cross-machine loops remain the agentbox/crabbox backend work (CLAWP-065/052).
