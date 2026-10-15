---
baseline_ref: '7331669'
complexity: m
created: '2026-10-15'
id: CLAWP-152
predictions:
  complexity: m
  confidence: 2
  duration_min: 120
  filled_by: agent
  pre_mortem: 'Most likely failure: the dashboard couples to unstable CLI JSON shapes
    and breaks on each clawpm release; mitigate with a versioned export or schema-pinned
    contract tests.'
  prediction_id: 898f1eb41dd349d58217eba9dac0d4a1
  success_criteria:
  - WHEN the dashboard/os project is scheduled THEN a stable documented JSON read
    contract for tasks, work-log and reflect scorecard exists and the dashboard renders
    from it without writing to clawpm state
priority: 9
tags:
- pending
updated: '2026-10-15'
---
# PENDING: clawpm visibility UI via operator's dashboard/os project reading clawpm JSON (revisit later)

Operator decision 2026-10-15: the built-in clawpm serve dashboard stays read-only and parked (CLAWP-078). The visibility UI will come from the operator's planned dashboard/os project, which reads clawpm JSON output (CLI --format json and the MCP server). This task is a revisit marker, not active work. When revisited: define the JSON read contract the dashboard needs (tasks list, work-log, reflect scorecard, mission state), decide whether clawpm exposes a stable export command or the dashboard shells out to the CLI, and keep clawpm serve either as the reference implementation or retire it.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

