---
complexity: m
created: '2026-05-27'
id: CLAWP-040
predictions:
  approach: summarize aggregates _compute_deltas across the reflections JSONL bucketed
    by complexity / confidence / agent_profile, flagging dirty actuals (no duration);
    suggest wraps the existing find_reference_tasks + applies the learned ratio. Deterministic,
    no model call. Falls back to global ratio when a bucket has n<5.
  complexity: m
  confidence: 3
  duration_min: 300
  files_scope:
  - src/clawpm/reflect.py
  - src/clawpm/cli.py
  - tests/test_reflect_summarize.py
  filled_by: operator-edited
  hypothesis: If reflect summarize quantifies the predicted/actual duration ratio
    and reflect suggest deflates new estimates by the learned ratio, the days-vs-hours
    estimate inflation gets measured and auto-corrected from the corpus clawpm already
    collects.
  pre_mortem: Sparse corpus per bucket gives wide CIs; mitigate by falling back to
    the global ratio when bucket n<5 and flagging low-confidence actuals.
  predicted_iterations: 2
  reference_tasks:
  - CLAWP-019
  - CLAWP-034
  success_criteria:
  - reflect summarize outputs a per-bucket predicted/actual duration ratio table over
    done tasks, bucketed by complexity and confidence, flagging rows with missing/dirty
    actuals.
  - reflect suggest <complexity> returns a duration deflated by the learned ratio
    for that bucket, deterministically (no model call), falling back to the global
    ratio when bucket n<5.
  - Both round-trip on the existing reflections corpus with no schema change; agent_profile
    (CLAWP-038) is available as a segmentation dimension.
priority: 5
---
# Close the calibration loop: reflect summarize + reflect suggest

Build the Phase-2 reflect consumers that were stubbed. Engine already exists: find_reference_tasks (CLAWP-023) does similarity lookup + duration_ratio; _compute_deltas computes the ratios; write_reflection_event records task_done with predictions+actuals+deltas. summarize aggregates; suggest applies. Directly attacks the operator's days-vs-hours estimate inflation.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

