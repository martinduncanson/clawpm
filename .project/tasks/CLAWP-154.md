---
baseline_ref: ddbfe13
complexity: m
created: '2026-10-15'
id: CLAWP-154
predictions:
  complexity: m
  confidence: 2
  duration_min: 240
  filled_by: agent
  pre_mortem: 'Most likely failure: a second, parallel mechanism beside the inbox,
    delegability and decision-map human leaves. Mitigate with the STEP 0 overlap spike
    and by reusing the existing blocked note and unblock path.'
  prediction_id: d60a010d438442b6853ffb8f294aa05c
  success_criteria:
  - WHEN a task is blocked on a human decision THEN a structured question and options
    can be recorded (CLI and MCP) and are returned by a JSON needs-you query across
    projects, without adding a TaskState value
  - WHEN the human answers THEN the answer is recorded on the task, the block clears
    via the existing unblock path, and the question/options remain in the task history
priority: 5
tags:
- workbench
updated: '2026-10-15'
---
# Awaiting-human decision record: structured question and options on a blocked task, queryable as JSON (needs-you view)

Source: research entry clawpm-research-workbench-md-templates-adoption-verdict-for-clawpm (2026-10-15). Today a human-needed decision is free-text (clawpm log --blocker, blocked-state note) and lived only in chat and session manifests. Design steer: structured fields on a BLOCKED task (blocked_on: human, question, options, optional recommendation), NOT a new TaskState value (every state guard would need updating). STEP 0 (spike, before building): read CLAWP-022 mission mini-goals tagged agent|human, CLAWP-054 human delegability, CLAWP-111 decision-map human leaves (HITL never self-answers) and the existing inbox; if the frontier already surfaces human leaves, narrow the scope to a cross-project needs-you query plus the structured question/options on blocked tasks. Feeds the CLAWP-152 dashboard marker. Define clawpm own vocabulary; do not copy workbench field names.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

