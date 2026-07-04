---
baseline_ref: 0b307f2
complexity: s
created: '2026-07-03'
id: CLAWP-079
state: active
predictions:
  confidence: 4
  duration_min: 120
  filled_by: agent
  success_criteria:
  - One canonical parse_frontmatter(text) -> (dict, body) helper (plus serializer
    if warranted) used by all previous hand-rolled sites
  - Grep for text.split with three-dash marker finds zero remaining hand-rolled parses
    outside the helper
  - Malformed-frontmatter behaviour is defined once (documented) and covered by tests
  - Full suite passes; zero behaviour change
priority: 5
scope:
- src/clawpm/
---
# Shared parse_frontmatter helper (dedup 15 hand-rolled sites)

Audit 2026-07-03 (code-health): the frontmatter parse dance - text.split("---", 2) + yaml.safe_load(parts[1]) or {} - is hand-rolled ~15x across 8 modules: models.py:508,781; tasks.py:312,1036,1201; mission.py:129,412,470; emit_tree.py:481,558,1275; doctor_apply.py:79; research.py:174; cli.py:847. Highest-value dedup in the repo: one place for encoding policy, malformed-input policy, and the CLAWP-045/046 errors=replace discipline.

SPEC: add the helper (models.py or a new frontmatter.py), migrate call sites mechanically one module per commit, preserve each site''s current malformed-input behaviour unless identical semantics can be proven (surgical - do NOT unify divergent edge-case behaviour in this task; note divergences for follow-up).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

