---
created: '2026-05-24'
id: CLAWP-023
predictions:
  approach: 'New find_reference_tasks(predictions, k=3) in reflect.py. Walk ~/clawpm/reflections/*.jsonl,
    filter task_done events, score similarity vs the proposed predictions, return
    top-k with actuals. CLI integrates into tasks add output: when --reference-task
    not specified, include ''suggested_references'' in the JSON response.'
  complexity: m
  confidence: 3
  duration_min: 180
  files_scope:
  - src/clawpm/reflect.py
  - src/clawpm/cli.py
  - tests/test_reference_tasks.py
  filled_by: agent
  pitfalls: Similarity heuristic could be too narrow (no matches) or too broad (everything
    matches). Need to tune weights. Reading all JSONLs could be slow on a large corpus.
  pre_mortem: 'Most likely failure: similarity scoring matches the wrong axis (e.g.
    all tasks with same complexity tier show up regardless of actual relevance). Mitigation:
    weighted multi-axis scoring with clear unit tests.'
  predicted_iterations: 2
  success_criteria:
  - criterion: find_reference_tasks returns top-k results sorted by similarity score
    gradeable_signal: pytest hits 3 reference tasks correctly ranked for a synthetic
      corpus
  - criterion: tasks add surfaces suggestions when --reference-task absent
    gradeable_signal: JSON output includes suggested_references list with task_id
      + similarity_score + duration_ratio_observed
  - criterion: 'Performance: <100ms on a 200-event corpus'
    gradeable_signal: time-stamped pytest
priority: 5
---
# Reference-task surfacing at predict-time (calibration corpus connect)

When 'clawpm tasks add' is called WITHOUT --reference-task, query the reflection corpus to surface 1-3 prior similar tasks with their actuals. Anchors new predictions to reference class instead of pure inside view. Matching: same complexity tier + scope-glob overlap + framework overlap + success-criteria text similarity (Jaccard or similar simple heuristic).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

