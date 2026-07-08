---
baseline_ref: 8592c46
created: '2026-07-05'
id: CLAWP-097
predictions:
  approach: 'Add missing entries to the All commands section: --tag (CLAWP-069), tasks
    archive (CLAWP-085), bulk varargs on done/start/block/unblock (CLAWP-083), python
    -m clawpm (CLAWP-072-005), updated timestamp field (CLAWP-086), query filters
    --text/--priority/--parent/--linked (CLAWP-082 once merged)'
  complexity: s
  confidence: 5
  duration_min: 45
  filled_by: agent
  success_criteria:
  - Every CLI command/flag shipped in this campaign (069/082/083/085/086/072-005)
    appears in README's All commands section
priority: 4
updated: '2026-07-08'
---

# README command reference is stale (missing tags/archive/bulk-state/python -m/updated-field)

Add missing entries to README's All-commands section: --tag (CLAWP-069), tasks archive (CLAWP-085), bulk varargs on done/start/block/unblock (CLAWP-083), python -m clawpm (CLAWP-072-005), updated timestamp field (CLAWP-086), query filters --text/--priority/--parent/--linked + wiki-link backlinks (CLAWP-082), --all-projects (CLAWP-084 once merged).

BROADENED SCOPE (operator, 2026-07-06): also sweep AGENTS.md.template and any other root-level *.md (excluding ROADMAP.md/CHANGELOG.md, which are CLAWP-076's job) for stale command syntax or missing agentic-layer commands, same treatment CLAWP-073 already gave SKILL.md. Spot-check every documented command against live clawpm <cmd> --help output -- don't guess syntax from memory.

SEQUENCING: CLAWP-084 and CLAWP-085 are still landing as of 2026-07-06 -- rebase onto fork/main and re-verify their final shipped command surface (--all-projects flag shape, tasks archive flags) before finalizing, rather than guessing ahead of their merge.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

