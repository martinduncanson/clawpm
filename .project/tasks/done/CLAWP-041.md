---
created: '2026-06-01'
id: CLAWP-041
predictions:
  approach: Add an opt-in refutation pass in stop_condition.py that fires ONLY on
    the ok=true->close transition, gated by --confirm-close flag / CLAWPM_CONFIRM_CLOSE
    env / auto-on when task confidence>=4. A distinct-lens refuter tries to disprove
    completion with default-to-refuted bias; survives->close, refuted->re-block with
    refuter reason. Single-call block path unchanged. Wire into hook eval-stop, agent
    dispatch, and dispatch.py command builder.
  complexity: m
  confidence: 3
  duration_min: 180
  filled_by: agent
  pitfalls: refuter and grader share a lens and rubber-stamp; or block-path call count
    regresses
  success_criteria:
  - 'A transcript that CLAIMS but does not EVIDENCE a criterion is caught by the refuter
    where the base judge passes it: regression test with a canned over-charitable
    transcript asserts confirmed-close verdict ok==false'
  - 'Block-path (base ok==false) call count unchanged: test asserts exactly one judge
    invocation per non-closing Stop'
  - 'Refutation fires at most once per close and only when base verdict ok==true:
    test asserts refuter invoker not called when base verdict is not-ok'
priority: 2
---
# Adversarial confirm-close tier for the rubric judge



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

