---
complexity: s
created: '2026-05-15'
id: CLAWP-001
predictions:
  approach: 'Add ''observation'' to the click.Choice for --type; add repeatable --tag
    flag; store as tags: List[str] in JSONL entries; default to [] on existing entries.
    Update issues list to filter by --tag and accept observation in --type filter.'
  complexity: s
  confidence: 4
  duration_min: 30
  files_changed: 3
  files_scope:
  - src/clawpm/cli.py
  - tests/test_issues*.py
  filled_by: agent
  hypothesis: If issues add accepts the form documented in global CLAUDE.md, doctrine
    docs become executable rather than aspirational
  pre_mortem: 'Most likely failure: forgetting cross-doc audit. Global CLAUDE.md,
    ~/.claude/skills/**, F:/Git/sysops/**, and various memories all reference the
    old form. After CLI lands, grep for ''issues add --type'' across those trees to
    catch sites needing reword.'
  reference_tasks:
  - CLAWP-000
  success_criteria:
  - clawpm issues add --type observation --tag depth-warning succeeds verbatim
  - clawpm issues list --tag depth-warning filters correctly
  - 'Existing issues.jsonl entries load with tags: [] defaulted'
  - Global CLAUDE.md doctrine form works without reword
  unknowns: Whether existing JSONL entries need a migration pass or whether read-with-default
    suffices. Whether 'observation' should also be valid in 'issues list --type' filter
    (probably yes).
priority: 5
---
# Extend clawpm issues add with observation type + --tag flag



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

