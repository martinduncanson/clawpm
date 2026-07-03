---
baseline_ref: 0b307f2
complexity: s
created: '2026-07-03'
id: CLAWP-076
predictions:
  confidence: 4
  duration_min: 90
  filled_by: agent
  success_criteria:
  - Root CHANGELOG.md exists, seeded from the ROADMAP phase table + merged-PR history,
    with an Unreleased section
  - Git tag exists on the fork matching pyproject version
  - ROADMAP phase summary reflects reality (no shipped phase marked in-flight; the
    stale 2026-06-05 Phase 2 gate resolved)
  - Stale counts corrected wherever they appear (test count is 1130 not 842)
priority: 4
scope:
- CHANGELOG.md
- ROADMAP.md
- pyproject.toml
---
# Release discipline: CHANGELOG, version tag, ROADMAP refresh

Audit 2026-07-03: version frozen at 0.1.0 since inception, ZERO git tags despite 35 merged PRs / ~70 done tasks; no feature CHANGELOG (archive/CHANGELOG.md only records file moves); ROADMAP.md phase summary is the de-facto changelog and is stale (Phases 1.6/1.7 marked in flight though long shipped; Phase 2 gated on a 2026-06-05 checkpoint four weeks past). Anyone installing from git cannot tell what they have.

SPEC: (1) write CHANGELOG.md (Keep-a-Changelog shape) seeded from ROADMAP phases + PR history; (2) decide version: tag v0.1.0 as-is or bump to 0.2.0 given the agentic layer shipped - recommend 0.2.0; (3) update pyproject + tag on fork; (4) refresh ROADMAP; (5) adopt the discipline: bump + CHANGELOG entry per merged feature PR (add to the codex-review briefing checklist for this repo).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

