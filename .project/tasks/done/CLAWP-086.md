---
baseline_ref: 0b307f2
complexity: s
created: '2026-07-03'
id: CLAWP-086
project: clawpm
predictions:
  confidence: 4
  duration_min: 90
  filled_by: agent
  pre_mortem: 'Most likely failure: a mutator writes frontmatter through a path that
    bypasses the shared serializer and misses the stamp; pairs naturally with the
    parse_frontmatter helper task - sequence after it'
  success_criteria:
  - 'Every mutating path (edit, state, log-attach, decompose, split) stamps updated:
    ISO date in frontmatter; add sets it equal to created'
  - tasks show/list surface updated; doctor stale-task check prefers updated over
    file mtime
  - Round-trip preserved by Task.from_file; tests cover each mutator stamping
priority: 6
scope:
- src/clawpm/models.py
- src/clawpm/tasks.py
updated: '2026-07-10'
---
# Add updated timestamp to task frontmatter

Audit 2026-07-03 (CLI/UX): tasks carry created only - no updated. Doctor stale-task detection falls back to file mtime, which lies after git operations, syncs, or external edits. DELIBERATELY OUT OF SCOPE: due dates on tasks - missions carry deadlines; per-task due dates invite calendar-shaped scope creep with no current pull (revisit only on demonstrated need).

SPEC: stamp updated on every mutator; sequence AFTER the parse_frontmatter helper so the stamp lives in one serializer.

## Acceptance Criteria

- [ ] Criterion 1: Every mutating path (edit, state, log-attach, decompose, split) stamps updated: ISO date in frontmatter; add sets it equal to created
- [ ] Criterion 2: tasks show/list surface updated; doctor stale-task check prefers updated over file mtime
- [ ] Criterion 3: Round-trip preserved by Task.from_file; tests cover each mutator stamping

## Notes

