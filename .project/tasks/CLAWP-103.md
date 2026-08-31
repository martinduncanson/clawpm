---
baseline_ref: b675a5e
created: '2026-07-14'
id: CLAWP-103
predictions:
  approach: Fable orchestrator + Sonnet readers per repo; output = ADR-style merge
    plan + seeded implementation tasks
  complexity: l
  confidence: 3
  duration_min: 240
  filled_by: agent
  pre_mortem: 'Risk: producing a parallel planning system instead of extending clawpm
    - the WONT-DO ledger row exists precisely to prevent this'
  success_criteria:
  - Written merge plan with adopt/adapt/skip verdict per mechanic and seeded follow-up
    tasks
priority: 5
updated: '2026-07-14'
---
# Fable session: clawpm vs wayfinder/openspec/feature-dev/osmani agent-skills - merge plan

Operator-directed 2026-07-14: full Fable-orchestrated comparison session (Fable reasoning <=high per routing policy) across clawpm, mattpocock wayfinder (decision tickets + fog-of-war + one-ticket-per-session + charting/working phases), OpenSpec, feature-dev plugin, and addyosmani/agent-skills (/spec /plan /build slash commands + specialist personas). Deliverable: gap analysis + merge plan - which mechanics enter clawpm core vs clawpm-planner vs get skipped, incl. Pocock-style personas (code-reviewer/security-auditor -> code-quorum instead?) and slash-command ergonomics. Inputs to gather first: osmani-scout + superpowers-scout reports from session d94ce3c4 (2026-07-14), decisions.md WONT-DO row on openspec (revisit clause), specs/spec-conformance-lens.md in code-quorum. House rule from 3.1 drift complaint: every grilling/planning conversation must end by writing decisions to durable state (task body/spec/decisions.md/gbrain).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

