---
baseline_ref: c1ccc71
created: '2026-07-06'
id: CLAWP-100
predictions:
  approach: 'Sidebar idea from repo-research on hesamsheikh/awesome-openclaw-usecases''
    project-state-management use case (2026-07-06). NOT a clawpm codebase change --
    an operator-side automation recipe built entirely on clawpm''s EXISTING primitives:
    tasks list --all-projects (CLAWP-084), context, log tail, reflect summarize. Sketch:
    a scheduled (schedule skill / cron) job that pipes clawpm''s cross-project JSON
    into a Slack post -- what happened, what''s next, what''s blocked, across the
    whole portfolio. To be scoped WITH the operator in a future session (what channel,
    what cadence, what''s actually worth surfacing vs noise) rather than built speculatively.'
  complexity: s
  confidence: 2
  duration_min: 60
  filled_by: agent
  pre_mortem: 'Risk: over-building a digest nobody reads; scope the conversation to
    confirm real pull before implementing anything'
  success_criteria:
  - 'A concrete spec agreed with the operator: cadence, destination (Slack channel),
    exact clawpm commands/fields consumed, and a decision on whether it''s worth building
    at all'
priority: 7
updated: '2026-07-06'
---
# Explore: cron-driven cross-project standup digest using clawpm's own JSON output



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

