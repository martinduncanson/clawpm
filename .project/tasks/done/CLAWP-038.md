---
complexity: s
created: '2026-05-27'
id: CLAWP-038
predictions:
  approach: 'Add agent_profile: str|None = None to Task (models.py), shape modeled
    on agenticq AgentCard.capability; serialize in frontmatter via to_dict/from_file/from_dict;
    dispatch_agent maps profile->subagent_type; record profile in the iteration_event
    for calibration segmentation. Absent field = None = current generic behaviour.'
  complexity: s
  confidence: 4
  duration_min: 120
  files_scope:
  - src/clawpm/models.py
  - src/clawpm/agent.py
  - src/clawpm/dispatch.py
  - tests/test_dispatch.py
  filled_by: operator-edited
  hypothesis: If a task carries an agent_profile capability hint, dispatch can route
    to the right specialist subagent AND calibration can segment predicted-vs-actual
    by profile, revealing which profiles beat generic dispatch at which complexity.
  pre_mortem: Back-compat break if from_file chokes on the absent field on legacy
    task files; default None and a tolerant parse guard it.
  predicted_iterations: 1
  reference_tasks:
  - CLAWP-037
  - CLAWP-017
  success_criteria:
  - agent_profile set on a task flows through to the dispatched subagent_type and
    appears in the iteration_event payload.
  - 'Calibration export gains a profile dimension: predicted-vs-actual can be grouped
    by agent_profile.'
  - Legacy task files with no agent_profile field load unchanged (field defaults to
    None, no parse error) - asserted by test.
priority: 5
---
# agent_profile capability field on tasks (modeled on agenticq AgentCard)

Ride-along to CLAWP-037. agenticq already realised this as AgentCard (capability + trust_tier + risk + requires_approval); model clawpm agent_profile on that shape so the two stay convergent if clawpm ever integrates agenticq as an execution spine. Independent of CLAWP-037 - can land first.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

