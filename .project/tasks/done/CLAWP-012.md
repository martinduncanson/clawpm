---
created: '2026-05-22'
id: CLAWP-012
predictions:
  approach: Reuse the existing subagent-judge SKILL.md + judge-prompt.md template.
    Build 3 synthetic clawpm-task fixtures (each with a deliverable file + success_criteria).
    Invoke judge via Task tool with subagent_type=general-purpose using Haiku. Capture
    verdict + reasoning + cost. Report calibration delta vs predicted.
  complexity: m
  confidence: 4
  duration_min: 60
  files_changed: 2
  files_scope:
  - .claude/skills/subagent-judge/**
  filled_by: agent
  hypothesis: 'If subagent-judge correctly classifies all 3 adversarial scenarios
    (FAIL/UNTESTABLE/PASS), the discipline is ready to ship as a reviewer-triangle
    peer reviewer. Calibration data: Haiku judge cost ~$0.025 per verdict (prior smoke).'
  pre_mortem: 'Most likely failure: Haiku is too lenient (false-positive PASS on the
    broken deliverable in Test 1) — would force redesign of judge prompt or model
    bump. Secondary: UNTESTABLE class isn''t well-modeled in the template, judge confabulates
    a PASS. Mitigation for both: log the verdict reasoning chain to identify which
    prompt cue failed.'
  reference_tasks:
  - CLAWP-011
  success_criteria:
  - Test 1 verdict = FAIL with criterion-by-criterion evidence
  - Test 2 verdict = UNTESTABLE not PASS
  - Test 3 verdict matches independent assessment of deliverable; cost <$0.05; wall
    <2min
priority: 5
---
# subagent-judge adversarial test run (Tests 1-3)

Run three adversarial tests against the subagent-judge skill prototyped earlier this session. Test 1: deliberately-broken deliverable (intentional failure of one or more success_criteria) — judge MUST return FAIL with concrete evidence per criterion. Test 2: vague/untestable success_criteria — judge MUST return UNTESTABLE rather than confabulating PASS. Test 3: end-to-end — dispatch real Sonnet subagent to deliver work against a clawpm task, judge with Haiku, compare verdict against deliverable. Capture cost ($), wall time, verdict accuracy. Decide: ship to skill catalog or iterate.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

