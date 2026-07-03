---
complexity: m
created: '2026-05-19'
depends:
- CLAWP-006
id: CLAWP-010
predictions:
  approach: 'Phase A (design): decide architecture. Two paths: (1) new ''subagent-judge''
    skill that reads clawpm task success_criteria, takes deliverable, calls Haiku
    4.5 with structured-output prompt, returns pass/fail JSON. (2) Extend codex-review
    PRE-REVIEW step #3 with --criteria-from-task CLAWP-N flag — reuses existing PRE-REVIEW
    dispatch surface, less new skill sprawl. Phase B (prototype): build path (2) first
    since it''s lighter. Phase C (real-task validation): run on one CLAWP task with
    sharp criteria + Sonnet subagent dispatch. Compare Judge verdict to operator''s
    manual review. Phase D (decide): keep, kill, or expand based on signal.'
  complexity: m
  confidence: 2
  duration_min: 180
  files_changed: 5
  files_scope:
  - skills/subagent-judge/
  - C:\Users\Martin Workspace/.claude/skills/codex-review/SKILL.md
  filled_by: agent
  hypothesis: 'Haiku as Judge against named criteria catches deliverable drift (subagent
    reports ''done'' but missed criterion #3) that PRE-REVIEW alone misses because
    PRE-REVIEW reviews diff quality, not criterion satisfaction. Sample size 1-3 tasks
    tells us if it pays off.'
  pre_mortem: 'Most likely failure: Haiku rubber-stamps complex deliverables. Defense:
    Judge prompt requires explicit reasoning per criterion, not just verdict. Test
    signal: if Judge says PASS but operator finds it FAIL, the prompt is too soft.
    Secondary failure: overlap with PRE-REVIEW becomes confusing — two gates doing
    similar things. Defense: explicitly decide in Phase A whether Judge is a SEPARATE
    gate (post-PRE-REVIEW) or REPLACES PRE-REVIEW for criteria-bearing tasks.'
  reference_tasks:
  - CLAWP-001
  success_criteria:
  - Judge prompt produces structured verdict per criterion (PASS/FAIL + reasoning)
  - 'Test on one real task: Judge verdict matches operator''s manual judgment ≥80%
    of criteria'
  - Judge runs in <30s and costs <$0.01 per task on Haiku 4.5
  - 'Failure mode caught: vague success_criteria can''t be judged — Judge returns
    ''criterion not testable, sharpen first'''
  unknowns: (1) Whether Haiku 4.5 quality is sufficient on judgment tasks vs Sonnet
    — needs benchmarking. (2) Whether ROADMAP doctrine ('clawpm is state substrate,
    not orchestration') allows the Judge invocation to live in a clawpm subcommand
    OR forces it to live entirely outside clawpm. (3) How to dispatch the Judge from
    within an active subagent context vs from the parent orchestrator — invocation
    model matters.
priority: 5
---
# Subagent Judge pattern: prototype Haiku-as-judge against success_criteria; decide skill vs PRE-REVIEW extension



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

