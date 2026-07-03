---
baseline_ref: 01ac8ae
complexity: m
created: '2026-06-26'
id: CLAWP-070
predictions:
  confidence: 2
  duration_min: 180
  filled_by: agent
priority: 5
---
# loop: bounded recurring task execution command

GOAL: a 'clawpm loop' command for bounded recurring execution of a task/dispatch until a stop condition — adapted from claude-task-master's loop command (research: .project/research/2026-06-26_...). clawpm already has dispatch + the Stop-hook rubric judge + thrashing detection (CLAWP-062); 'loop' would compose them into an operator-facing 'keep working this until rubric-satisfied or N iterations / budget exhausted' primitive, rather than the operator re-invoking dispatch each round.

EVALUATE FIRST (pre-mortem): heavy overlap with existing dispatch + Stop-hook iteration + CLAWP-062 thrashing guard. Confirm 'loop' adds a distinct, non-redundant surface (e.g. iteration cap + budget cap + per-iteration progress log) before building. If it's just a thin wrapper, fold the missing knob into dispatch instead of a new command.

SCOPE (if justified): bounded loop over a task's dispatch with --max-iterations / --max-budget / stop-on-rubric-satisfied; reuse the thrashing detector; emit an iteration log (CLAWP-019 outcome iteration log already exists — reuse).

SUCCESS CRITERIA: (1) a decision recorded (build vs fold-into-dispatch) with rationale; (2) if built: loop terminates on rubric-satisfied OR max-iterations OR budget, with an iteration log; tests for each termination path.

OUT OF SCOPE: cross-machine loop (that's the agentbox/crabbox backend work, CLAWP-065/052).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

