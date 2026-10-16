---
baseline_ref: 1338bc6
complexity: m
created: '2026-10-15'
id: CLAWP-157
predictions:
  complexity: m
  confidence: 3
  duration_min: 180
  filled_by: agent
  pre_mortem: 'Most likely failure: a second divergent write path beside the CLI helpers;
    mitigate by a services wrapper both call'
  prediction_id: 03f3e26d7f964af8a58c91b6d1402b67
  success_criteria:
  - WHEN an agent calls MCP tasks_state with question, options, recommendation or
    answer THEN the record is written exactly as the CLI does and tasks list needs-human
    returns it
priority: 7
updated: '2026-10-16'
---
# CLAWP-154 follow-up: MCP tasks_state fields and SKILL.md mirror for the human-decision record

Source: PR #122 (CLAWP-154). Deferred: a services/tasks.py wrapper over record_human_question, record_human_answer and clear_stale_human_decision, the MCP tasks_state fields (mcp_server.py), and the clawpm SKILL.md command table (skill mirror is untracked in the checkout the harness reads; see memory skill-mirror auto-sync).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

