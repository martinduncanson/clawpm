---
baseline_ref: ae3cbbd
created: '2026-07-04'
id: CLAWP-095
predictions:
  complexity: s
  confidence: 3
  duration_min: 90
  files_scope:
  - src/clawpm/research.py
  - src/clawpm/models.py
  filled_by: agent
  pre_mortem: Surfacing malformed entries changes research list output schema; risk
    is breaking consumers that assume a flat list
  success_criteria:
  - list_research/get_research no longer silently drop items whose from_file raises;
    malformed files are surfaced (count or list) in research list JSON+text
  - 'add_research uniques the frontmatter id: field (not just filename) so two same-day
    same-slug titles get distinct ids'
  - from_file no longer swallows yaml.YAMLError into empty-frontmatter+raw-content
    silently; parse failure is surfaced not hidden
priority: 5
---
# Research read-path hardening (deferred from CLAWP-087 review)

Three pre-existing findings surfaced by Codex+Grok review of PR #43 (CLAWP-087), deferred as out-of-scope for the template fix: (1) broad 'except Exception: continue' in list_research/get_research silently drops unparseable research files - now more impactful since research list is the primary rot-surface; (2) research_id generated from title slug is not uniqued (only the filename gets a counter) so same-day same-slug titles collide on frontmatter id:; (3) from_file swallows yaml.YAMLError with bare pass and proceeds with empty frontmatter + raw text as content. All predate the CLAWP-087 diff; belong with the parse-hardening lane (CLAWP-079 parse_frontmatter helper, now merged).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

