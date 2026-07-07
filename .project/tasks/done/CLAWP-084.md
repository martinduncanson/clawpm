---
baseline_ref: 0b307f2
complexity: s
created: '2026-07-03'
id: CLAWP-084
predictions:
  confidence: 4
  duration_min: 90
  filled_by: agent
  success_criteria:
  - clawpm tasks list --all-projects returns tasks across every active project, each
    row carrying project_id, composable with state/tag/text filters
  - Default remains single-project (no behaviour change without the flag)
  - Tests cover multi-project fixture incl. same-numeric-id tasks in different projects
priority: 6
scope:
- src/clawpm/cli.py
- src/clawpm/tasks.py
updated: '2026-07-07'
---
# Cross-project tasks list (--all-projects)

Audit 2026-07-03 (CLI/UX): only next / projects next aggregate across projects; there is no portfolio-wide task VIEW. Operators ask what is open everywhere by running the CLI once per project.

SPEC: --all-projects flag on tasks list (and probably tasks bare alias) iterating discover_projects active set. Respect per-project prefixes so ids stay unambiguous (cross-project id isolation class - see memory: 9 corruption sites; this is read-only but the display must carry project scope explicitly). Sequence AFTER the query/filtering task so filters compose from day one.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

