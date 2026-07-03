---
baseline_ref: 84a3869
complexity: m
created: '2026-06-12'
id: CLAWP-064
predictions:
  complexity: m
  confidence: 3
  duration_min: 240
  filled_by: agent
  reference_tasks:
  - CLAWP-056
  - CLAWP-059
  success_criteria:
  - an emit-tree document with leaves nested via parent_ref (>=2 levels) persists
    the full hierarchy atomically, each non-root leaf parented to its parent_ref's
    minted task (covered by a multi-level fixture); the v1 fail-closed rejection is
    removed
priority: 6
---
# emit-tree: support in-document hierarchical nesting via parent_ref (multi-level trees in one emit)

CLAWP-056's emit-tree v1 ships FAIL-CLOSED on in-document parent_ref: a leaf with a non-null parent_ref is rejected, because v1 emits a single level (all leaves under the root). Depth is achievable across multiple calls via attach_to. This task adds true in-document nesting: a leaf whose parent_ref points at another leaf in the same document is emitted as a child of that leaf's minted task, to arbitrary depth, within the same atomic staging+promote. Requires: recursive child-ID prediction per parent, nested staging dirs mirroring the final tree, and parent-child frontmatter links at every level. The planner skill (CLAWP-059) wants this so a decomposition can emit a full multi-level tree in one shot rather than N attach calls. Keep the atomic all-or-nothing guarantee.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

