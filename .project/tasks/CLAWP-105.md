---
baseline_ref: b675a5e
created: '2026-08-13'
id: CLAWP-105
predictions:
  complexity: s
  confidence: 3
  duration_min: 45
  filled_by: agent
priority: 5
updated: '2026-08-13'
---
# Sweep residual codex-review->code-quorum refs (clawpm repo)

The code-quorum skill rename completed 2026-08-05 (identity+junction+~/.claude live refs done, PR#25 merged, commit cb60df5). Residual refs live in the CLAWPM repo, non-breaking: (1) FUNCTIONAL - clawpm-cowork SKILL.md (x2: skills/clawpm/skills/clawpm-cowork + skills/clawpm-cowork) clone commands point at github.com/martinduncanson/codex-review.git into skills/codex-review/ dir - GitHub redirects the renamed repo so it still works, but update URL->code-quorum.git + dir->code-quorum; (2) DOC - docs/playbooks/codex-fix-dispatch.md cross-ref paths; (3) LEAVE - historical .project/tasks/done/*.md records (CLAWP-003/010) - don't rewrite history. Also consider: repo rename on GitHub is done (remote=code-quorum), verify no other repo hardcodes the old skill path.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

