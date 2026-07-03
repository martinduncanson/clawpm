---
complexity: m
created: '2026-06-11'
id: CLAWP-053
predictions:
  complexity: m
  confidence: 3
  duration_min: 180
  files_changed: 5
  filled_by: agent
  pre_mortem: most likely failure = 'rejected' conflated with 'blocked'/'cancelled'
    existing states, muddying state-machine semantics; mitigate by enumerating ALL
    states up front per the codex_review_design_lessons memory
  reference_tasks:
  - CLAWP-050
  success_criteria:
  - a task/idea can be moved to a 'rejected' terminal state carrying a required free-text
    rationale; rejected items are excluded from 'next'/open listings by default but
    queryable
  - a programmatic surface (CLI/json) returns the reject set for a project so a generator
    can dedup candidates against it; a second decomposition run does not re-emit a
    rejected candidate (covered by a test fixture)
priority: 5
---

# Won't-do / considered-and-rejected ledger (project-agnostic)

A first-class terminal task state (or sibling record) for 'considered and rejected, with one-line rationale' so a decomposition/ideation pass does not re-propose the same discarded idea every run. Domain-agnostic: applies to a rejected code refactor OR a rejected research direction. Adapted from shadcn/improve's 'Findings considered and rejected' index section (records rejections so they are not re-audited) and BMAD's elicitation discipline. Distinct from 'blocked' (terminal-needs-human) and 'done'. The decomposition pipeline (sibling task) MUST consult this ledger before emitting candidates. Include: rationale field (required), optional supersedes link, and a query surface so a generator can diff candidates against the reject set.

ENRICHMENT (mattpocock/triage .out-of-scope KB, 2026-06-11): the ledger is NOT just exact-match dedup. mattpocock's triage skill writes wontfix enhancements to a durable .out-of-scope/ knowledge base and, on every NEW candidate, surfaces any prior rejection that RESEMBLES it. Adopt the same: when the generator produces a candidate, surface resembling prior rejections (semantic/fuzzy match, not just identical title) so near-duplicates of already-rejected ideas are caught and the operator is reminded WHY it was rejected before re-litigating. Convergent with OpenSpec's archive lifecycle and improve's rejected-section — three sources, same pattern.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

