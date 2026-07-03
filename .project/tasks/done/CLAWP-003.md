---
complexity: s
created: '2026-05-15'
id: CLAWP-003
predictions:
  approach: 'Add a new bootstrap step (after CLI install, before clawpm setup --check):
    ''git clone https://github.com/martinduncanson/codex-review.git ~/.claude/skills/codex-review
    || (cd ~/.claude/skills/codex-review && git pull)'' — idempotent. Updates the
    Steps section + Environment table if needed. Updates Troubleshooting if ''codex-review
    missing on Cowork'' becomes a known failure mode.'
  complexity: s
  confidence: 4
  duration_min: 15
  files_changed: 1
  files_scope:
  - skills/clawpm-cowork/SKILL.md
  filled_by: agent
  hypothesis: If the codex-review skill auto-installs on Cowork bootstrap, the PRE-REVIEW
    + 3-5 Concerns discipline reaches every clawpm-tracked environment instead of
    just this workstation
  pre_mortem: 'Most likely failure: gh auth not yet completed on Cowork session when
    the clone fires (anonymous HTTPS clone works for public repos so should be fine,
    but if codex-review is later switched to private or moved into an org, the bootstrap
    breaks silently). Mitigation: keep public OR add a gh auth check before the clone
    step.'
  reference_tasks:
  - CLAWP-001
  success_criteria:
  - Bootstrap step idempotent (works on fresh AND re-runs)
  - Cowork session post-bootstrap can invoke codex-review skill via Skill tool
  - Step is documented in skills/clawpm-cowork/SKILL.md alongside the existing clawpm
    CLI install
  - Cowork-relevant subset of the codex-review propagation map (project-codex-review-skill-repo.md)
    flips from NOT-auto to auto
  unknowns: 'Whether to use the github.com URL or the operator''s preferred remote
    pattern (fork vs upstream). codex-review has no upstream yet, so this is moot
    today but worth a thought if a community fork emerges. Also: should the bootstrap
    pull every session (idempotent re-clone), or only clone if missing? Re-cloning
    every session wastes ~2s but keeps the skill fresh.'
priority: 5
---
# Extend clawpm-cowork bootstrap to install codex-review skill on ephemeral VMs



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

