---
created: '2026-05-22'
depends:
- CLAWP-016
- CLAWP-017
id: CLAWP-019
predictions:
  approach: Stop-hook evaluator (CLAWP-NEW-2) writes an iteration_event line to reflections
    JSONL on each invocation (whether ok=true or ok=false). Add predicted_iterations
    field to task frontmatter (default 1). At done, compute iterations_actual from
    JSONL count, surface delta in reflection event payload alongside duration_ratio
    and complexity_match.
  complexity: m
  confidence: 3
  duration_min: 180
  files_scope:
  - clawpm/reflections/*
  - clawpm/commands/state.py
  - tests/test_reflection.py
  filled_by: agent
  pitfalls: Iteration count may inflate when subagent thrashes; need to distinguish
    'genuine revision' from 'noise turn'. May need a separate 'thrash' tag (ralph-orchestrator
    pattern) for noise
  pre_mortem: 'Most likely failure: iteration_event noise — every time Claude self-reviews,
    an event fires, polluting the count. Mitigation: only count Stop-hook invocations
    (not self-review), and tag thrash-detected cycles separately.'
  success_criteria:
  - Reflection JSONL contains one iteration event per Stop-hook invocation
  - Final task_done event includes iterations_actual and (if predicted) iterations_ratio
    in deltas
  - clawpm reflect summarize (Phase 2 stub) surfaces iteration-count accuracy as a
    calibration dimension
  - Schema documented in SKILL.md alongside duration/complexity deltas
priority: 5
---
# Outcome iteration log: capture grader cycles as reflection events

When a task is dispatched under the iterate-grade-revise pattern (locally via the Stop-hook evaluator from CLAWP-NEW-2, OR via Managed Agents user.define_outcome if operator opts in), capture every grader cycle as an iteration event in clawpm/reflections/<task-id>.jsonl. Schema mirrors span.outcome_evaluation_end: {iteration, result, explanation, occurred_at}. Iteration count then becomes a predict-vs-actual axis: 'predicted 1 iteration, took 4 revisions' is calibration signal.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

