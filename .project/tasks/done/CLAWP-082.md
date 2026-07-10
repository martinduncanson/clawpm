---
baseline_ref: 0b307f2
complexity: m
created: '2026-07-03'
id: CLAWP-082
predictions:
  confidence: 3
  duration_min: 240
  filled_by: agent
  pre_mortem: 'Most likely failure: a persisted backlink index goes stale vs file
    edits made outside the CLI; mitigate by computing backlinks at read time first
    (portfolio is small) and only caching if measurably slow'
  success_criteria:
  - tasks list supports --text (substring/regex over title+body), --priority, --complexity,
    --parent, --limit, composable with --state and the CLAWP-069 --tag filter
  - Wiki-links like [[CLAWP-042]] or [[research-id]] in task/research/mission bodies
    are parsed into a links field, and tasks show / context return both links and
    linked_from (backlinks)
  - tasks list --linked CLAWP-042 returns every entity referencing it
  - Backlink index is DERIVED (rebuildable from files, never authoritative); a doctor
    check flags dangling wiki-links
  - Tests cover filter composition + backlink extraction round-trip
priority: 4
scope:
- src/clawpm/tasks.py
- src/clawpm/models.py
- src/clawpm/cli.py
updated: '2026-07-10'
---
# Task query/filtering + wiki-link backlinks (graph interlinking)

Audit 2026-07-03 (CLI/UX) + operator question re karpathy-style graph interlinking. Two halves:

(1) QUERY: tasks list filters only by --state (cli.py:1370-1398) - no text search, no priority/complexity/parent/limit filters despite rich frontmatter. Add composable filters; keep JSON-first.

(2) INTERLINKING: clawpm already has a TYPED graph (depends, parent/children, reference_tasks, supersedes, mission links, research link) but no freeform linking and NO BACKLINKS - if research entry X mentions CLAWP-042, an agent opening CLAWP-042 never learns X exists. Adopt the wiki-link convention: parse [[id]] from bodies across tasks/research/missions, expose links + linked_from in show/context, add --linked filter. HARD RULE (CLAWP-061 doctrine): readable files stay primary; the link index is derived and rebuildable - no graph DB, no viz. Typed edges (depends etc.) should ALSO surface in linked_from so backlinks unify both graphs.

COORDINATE with CLAWP-069 (tags): same list-filter surface - implement filter plumbing once. Consider extending emit-tree/planner to emit [[refs]] between sibling leaves for free cross-navigation.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

