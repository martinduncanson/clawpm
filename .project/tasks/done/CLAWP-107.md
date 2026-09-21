---
baseline_ref: 9109b88
created: '2026-09-01'
id: CLAWP-107
predictions:
  approach: Add .claude-plugin/marketplace.json at repo root (name=clawpm, single
    plugin entry, source='./', strict=false, skills=['./skills/clawpm']) mirroring
    the .claude-tools/session-tools precedent exactly (verified via gh api fetch of
    both existing marketplace.json files + official docs). Register in ~/.claude/settings.json
    extraKnownMarketplaces + enabledPlugins. Test the install path. Retire ~/.claude/skills/clawpm
    git-checkout clone and its scripts/git-hooks/post-merge sync hook.
  complexity: m
  confidence: 4
  duration_min: 90
  filled_by: agent
  success_criteria:
  - F:/Git/clawpm/.claude-plugin/marketplace.json exists, valid JSON, matches verified
    schema
  - clawpm@clawpm plugin installs and loads successfully in a fresh Claude Code session
    (skill triggers correctly)
  - C:\Users\Martin Workspace/.claude/skills/clawpm checkout and its post-merge hook
    are removed, with no remaining reference to the old mechanism
  - code-quorum review clean on both the clawpm-repo PR and the ~/.claude config change
    before considering done
priority: 4
updated: '2026-09-02'
---
# Package clawpm as its own Claude Code plugin marketplace, retire git-checkout skill mirror



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

