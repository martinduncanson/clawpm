---
created: '2026-05-25'
id: CLAWP-035
predictions:
  approach: 'Three deliverables: (a) document the Codex-fix dispatch pattern as a
    reusable template (notes/codex-fix-dispatch.md or in codex-review skill); (b)
    provide a worked example dispatching one real Codex-iteration loop via clawpm
    tasks dispatch with structured rubric; (c) add a tasks add --rubric-from-pattern
    codex-fix shortcut OR document the canonical rubric flags. The aim: zero net new
    feature code, max reuse of existing dispatch+judge primitives.'
  complexity: m
  confidence: 3
  duration_min: 120
  filled_by: agent
  pitfalls: Stop-hook condition evaluator may need a wait-for-codex bridge to grade
    against; subagent-driven Codex polling needs careful timeout handling so it doesn't
    deadlock the dispatch.
  pre_mortem: 'If this fails, most likely cause: the existing dispatch mechanism is
    designed around CLI-state-poll grading, not network-async-poll (Codex). May need
    a custom grader script or a polling wrapper inside the rubric''s gradeable_signal.'
  reference_tasks:
  - CLAWP-018
  - CLAWP-017
  success_criteria:
  - A clawpm tasks dispatch invocation that runs the Codex-fix loop against a test
    PR auto-terminates when Codex returns clean (without parent intervention)
  - Codex-fix-dispatch pattern documented as reusable (markdown file or skill update)
    with a worked rubric, the dispatch invocation, and the expected behavior
  - At least one real Codex round (test or production) successfully dispatched via
    the pattern with rubric+judge
priority: 3
---
# Goal+judge adoption: convert Codex-fix iteration to rubric-dispatch



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

