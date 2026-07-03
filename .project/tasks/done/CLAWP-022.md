---
created: '2026-05-24'
id: CLAWP-022
predictions:
  approach: 'New mission.py module + .project/missions/ dir + Mission dataclass. CLI:
    mission add/list/status/tasks. Mini-goals are regular tasks tagged with parent_mission
    + actor fields. mission status walks subtasks, reports % complete + binary outcome
    state. mission tasks --actor human filters.'
  complexity: l
  confidence: 2
  duration_min: 480
  files_scope:
  - src/clawpm/mission.py
  - src/clawpm/cli.py
  - tests/test_mission.py
  filled_by: agent
  pitfalls: Actor tagging for tasks may collide with existing subtask semantics. Mission
    deadline calculation needs careful date handling. Schema needs to round-trip through
    frontmatter cleanly.
  pre_mortem: 'Most likely failure: scope creep from trying to also build a dashboard
    view. Mitigation: CLI-only v1, no dashboard.'
  predicted_iterations: 2
  success_criteria:
  - criterion: clawpm mission add creates a mission with deadline + binary_outcome
      + 4-10 mini-goals
    gradeable_signal: JSON file written to .project/missions/<id>.md with mini-goals
      as subtasks
  - criterion: Mini-goals carry actor field (agent or human)
    gradeable_signal: task frontmatter has actor field; CLI filter works
  - criterion: mission status reports progress + binary outcome verdict
    gradeable_signal: JSON output includes complete_count, total_count, deadline_eta,
      outcome_status
priority: 5
---
# Mission Control: clawpm mission layer with mixed-actor mini-goals

New 'clawpm mission' command group above tasks. A mission = 4-week binary outcome decomposed into 4-10 mini-goals (subtasks with actor: agent|human field). Adapts Claude /goal Mission Control pattern (goals-long.txt) without the dashboard POST. Surfaces: 'clawpm mission add', 'clawpm mission status', 'clawpm mission tasks' (filtered by actor).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

