---
complexity: s
created: '2026-05-15'
id: CLAWP-004
predictions:
  approach: Copy ~/.claude/skills/clawpm-cowork/SKILL.md (with the CLAWP-003 edit)
    to F:/Git/clawpm/skills/clawpm-cowork/SKILL.md. Extend sync-runtime-clones.sh
    with a second MIRROR block that copies ~/.claude/skills/clawpm/skills/clawpm-cowork/SKILL.md
    → ~/.claude/skills/clawpm-cowork/SKILL.md after the repo sync. Commit, ff to main,
    push fork, run sync.
  complexity: s
  confidence: 4
  duration_min: 15
  files_changed: 2
  files_scope:
  - skills/clawpm-cowork/SKILL.md
  - sync-runtime-clones.sh
  filled_by: agent
  hypothesis: Bundling clawpm-cowork into the clawpm repo eliminates the un-versioned-skill
    class for this satellite, and the existing sync mechanism propagates updates the
    same way it does for the main clawpm skill
  pre_mortem: 'Most likely failure: forgetting the MIRROR step would leave the canonical
    edit landing inside the clawpm repo clone but the actual ~/.claude/skills/clawpm-cowork/SKILL.md
    untouched. Caught by the second success-criterion smoke-test: edit canonical →
    run sync → diff outer.'
  reference_tasks:
  - CLAWP-003
  success_criteria:
  - F:/Git/clawpm/skills/clawpm-cowork/SKILL.md exists and matches the current runtime
    version (with the codex-review clone block from CLAWP-003)
  - sync-runtime-clones.sh mirrors clawpm-cowork SKILL.md from the nested repo path
    to ~/.claude/skills/clawpm-cowork/SKILL.md
  - Re-running sync-runtime-clones.sh is idempotent (no diff on second invocation)
  - Future edits to F:/Git/clawpm/skills/clawpm-cowork/SKILL.md propagate to ~/.claude/skills/clawpm-cowork/SKILL.md
    via sync
  unknowns: Whether the existing MIRROR step for clawpm itself uses cp (yes, line
    52) or symlink. cp is fine for this purpose. Whether Claude Code's skill loader
    caches SKILL.md content between sessions — if yes, no impact (next session re-reads);
    if no, also no impact. Either way the in-place file becomes the source of truth
    via mirror.
priority: 5
---
# Bundle clawpm-cowork skill into F:/Git/clawpm/skills/ + extend sync-runtime-clones.sh mirror



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

