---
created: '2026-05-22'
id: CLAWP-016
predictions:
  approach: 'Add SuccessCriterion dataclass with optional gradeable_signal + comparator.
    Migration: existing string list parses as criterion-only. emit-rubric outputs
    markdown with one ## per criterion, gradeable signal as evidence requirement,
    comparator as pass condition. Backwards-compat tested via existing fixtures.'
  complexity: m
  confidence: 4
  duration_min: 180
  files_scope:
  - clawpm/models/*
  - clawpm/commands/tasks.py
  - tests/test_tasks.py
  filled_by: agent
  pitfalls: YAML serialisation of mixed string/struct lists; need careful migration
    of existing CLAWP-* tasks with freeform criteria
  pre_mortem: 'Most likely failure: existing tasks with freeform success-criteria
    break when parsed under new schema. Mitigation: parse-as-string fallback in the
    loader; test fixture covers both forms.'
  success_criteria:
  - Schema accepts both string and structured forms; CLI passes existing tests
  - emit-rubric output validates against user.define_outcome rubric requirements (gradeable,
    independent criteria)
  - 'Round-trip: rubric -> Stop-hook evaluator -> {ok, reason} JSON returns plausible
    verdicts on 3 sample tasks'
priority: 5
---
# Rubric-shaped success-criteria + clawpm tasks emit-rubric helper

Restructure task.success_criteria from freeform strings to structured {criterion, gradeable_signal, comparator} objects. Add 'clawpm tasks emit-rubric <id>' that renders the rubric as markdown compatible with both (a) Anthropic's Managed Agents user.define_outcome rubric field, and (b) a local Stop-hook condition evaluator prompt. Preserve backwards-compat: freeform strings still accepted, treated as criterion with no gradeable_signal. Schema lives in clawpm/models/task.py; emit-rubric helper in clawpm/commands/tasks.py.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

