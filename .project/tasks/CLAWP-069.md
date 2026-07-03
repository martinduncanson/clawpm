---
baseline_ref: 01ac8ae
complexity: m
created: '2026-06-26'
id: CLAWP-069
predictions:
  confidence: 4
  duration_min: 180
  filled_by: agent
priority: 5
---
# Tags / workstreams: cross-cutting task grouping

GOAL: add lightweight cross-cutting TAGS (a.k.a. workstreams) to clawpm tasks so work can be grouped/filtered across the existing project+state axes — e.g. tag a slice of tasks 'concurrency', 'mcp', 'q3-roadmap' and list/operate on them as a unit. Adapted from claude-task-master's tags/workstreams (research: .project/research/2026-06-26_claude-task-master-agentbox-eval-for-clawpm.md). clawpm currently groups only by project and parent/subtask; tags are an orthogonal grouping that's cheap and high-utility for multi-stream work.

SCOPE:
- Task frontmatter: optional repeatable 'tags: [..]' field (already have scope/depends/out_of_scope patterns to mirror).
- CLI: 'tasks add/edit --tag X' (repeatable); 'tasks list --tag X' filter (AND/OR semantics — decide); surface tags in list/context output.
- Core: tags are pure metadata (no FS-move, unlike state) — read/filter only; no lock interaction beyond the existing edit_task path.
- Consider a 'tags' listing command (distinct tags + counts) for discovery.

DECISIONS: single --tag repeatable vs comma-list; list filter AND vs OR vs both (--tag a --tag b => AND? --any-tag?); whether tags propagate parent->child.

SUCCESS CRITERIA: (1) a task can carry multiple tags, persisted in frontmatter, round-tripped by Task.from_file; (2) 'tasks list --tag X' returns exactly the tagged set; (3) tags render in list + context; (4) tests for persistence + filter semantics.

OUT OF SCOPE: tag-based dispatch/rollup; tag renaming/merging tooling (later if needed).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

