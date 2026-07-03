---
complexity: m
created: '2026-06-11'
depends:
- CLAWP-056
id: CLAWP-057
predictions:
  complexity: m
  confidence: 3
  duration_min: 180
  files_changed: 5
  filled_by: agent
  pre_mortem: most likely failure = reinvents SPEC.md or bloats core into a policy
    engine; mitigate by shipping it as a thin optional constraint-set consulted ONLY
    by the generator, never a standalone subsystem
  reference_tasks:
  - CLAWP-056
  - CLAWP-053
  success_criteria:
  - a project can declare named invariants (CLI/file); clawpm runs identically when
    none are declared
  - 'every task emitted by the decomposition pipeline is checked against the active
    invariants and a violation is flagged pre-emission (test fixture: a test-first
    invariant rejects a no-test leaf)'
  unknowns: Whether invariants are free-text (model-judged) or structured (code-checkable);
    likely a mix - keep code-for-facts where a check is mechanical, model-for-judgment
    only where genuinely ambiguous
priority: 5
---
# Constitution / governing-principles layer (project-agnostic invariants for the decomposition pipeline)

A persistent set of named, project-scoped INVARIANTS that constrain every task the decomposition pipeline (CLAWP-056) emits — distinct from SPEC.md (which is scope/what) and the mission layer (which anchors OUTCOMES). A constitution anchors CONSTRAINTS: e.g. "all code work is test-first", "knowledge-work deliverables must cite sources", "no task may exceed complexity l without an explicit split". Adapted from github/spec-kit's `/constitution` command (governing principles that guide all subsequent specs/tasks), generalised to be project-agnostic (applies to code OR knowledge work).

Keep it THIN per the lean-core directive: NOT a new subsystem — a small optional constraint-set, consulted only by the generator at emission time. clawpm runs identically with no constitution declared. A violation is flagged pre-emission (the generator either splits/fixes the candidate or routes it to the won't-do ledger, CLAWP-053). This is the constraint-anchoring complement to CLAWP-056's outcome-traceability.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

