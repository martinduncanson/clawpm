---
baseline_ref: 84a3869
complexity: m
created: '2026-06-12'
id: CLAWP-062
predictions:
  complexity: m
  confidence: 3
  duration_min: 180
  filled_by: agent
  pre_mortem: false-positive thrash flags on legitimate iterative work; mitigate via
    conservative default + per-task override + requiring no-rubric-progress alongside
    mod-count
  reference_tasks:
  - CLAWP-054
  success_criteria:
  - a dispatched agent modifying the same in-scope file beyond the configured threshold
    WITHOUT rubric progress is flagged/stopped as thrashing (test); threshold configurable
    per-task and globally
priority: 5
---
# Dispatch thrashing/runaway detection (elves-inspired) — stop an agent looping without progress

From aigorahub/elves: modified the same file N times without meaningful progress -> STOP. For unattended dispatch this stops an agent burning hours treating symptoms. Build on clawpm iteration log + predicted_iterations. DETERMINISTIC heuristic: configurable file-modification-count threshold (default ~5, per-task + global override), surfaced as a dispatch-health signal / auto-tripped stop_condition (composes with CLAWP-054). KEEP SIMPLE: mod-count is a proxy for no-progress; pair with rubric-not-advancing rather than a sophisticated progress detector.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

