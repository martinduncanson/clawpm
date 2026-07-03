---
complexity: m
created: '2026-06-11'
id: CLAWP-054
predictions:
  success_criteria:
  - tasks accept repeatable --out-of-scope and --stop-condition values, persisted
    on the task and rendered in the dispatch/agent preamble verbatim
  - a tripped stop_condition surfaces to the Stop-hook judge as a terminal 'report-back'
    outcome distinct from an unmet success_criterion (covered by a test)
  - a task carries delegability (agent|human|either, default either); the dispatch
    path refuses to auto-dispatch a 'human' leaf and surfaces it to the operator instead
    (covered by a test)
priority: 5
---

# Task/dispatch contract fields: out_of_scope + stop_conditions + delegability

clawpm success_criteria nails the DONE check but is thin on three contract concerns that keep a weak/cheap dispatched executor — or a knowledge-work delegate — from drifting or being mis-assigned. Add the following repeatable/scalar fields to the task schema and propagate them into the dispatch/agent preamble + the Stop-hook judge context. Project-agnostic throughout: NOT code-specific.

1. out_of_scope (repeatable) — the BOUNDARY: 'do NOT touch these adjacent things that look related'. Can be file globs OR named topics/deliverables. From shadcn/improve's plan-template 'Out of scope' list.

2. stop_conditions (repeatable, free-text) — the ESCAPE HATCH: 'if assumption X proves false, STOP and report instead of improvising'. A tripped stop_condition is a terminal BLOCKER, not a repairable drift (GSD-Pi's drift-vs-blocker distinction). The executor declares the trip (agent-reported flag, not judge-inferred — code-for-facts, model-for-judgment).

3. delegability: agent | human | either (scalar, default 'either') — WHO may execute this leaf. From mattpocock/triage's ready-for-agent vs ready-for-human distinction: some units genuinely need a human (judgment calls, external access, design decisions, manual testing / verification) and must NOT be silently auto-dispatched to a subagent that then flails. The dispatch path REFUSES to auto-dispatch a 'human' leaf (surfaces it to the operator instead); 'agent' and 'either' dispatch normally. The decomposition pipeline (CLAWP-056) sets this per emitted leaf. Project-agnostic: a knowledge-work leaf requiring a stakeholder decision is 'human' just as a code leaf requiring prod access is.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

