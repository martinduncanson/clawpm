---
baseline_ref: 00fe5f9
created: '2026-10-09'
id: CLAWP-144
priority: 5
updated: '2026-10-09'
---
# BOM-prefixed task files: Task.from_file / split_frontmatter treat them as frontmatter-less (repo-wide)

Codex r2 on PR #93 (CLAWP-111-002): a task file starting with a UTF-8 BOM is parsed as having no frontmatter by Task.from_file / split_frontmatter, so it loads with empty metadata (dir-form root id becomes _task). #93 made edit_fog preserve the BOM on write, but the reload still returns empty metadata, so tasks fog --add reports not_yet_specified [] for such a file. Pre-existing and repo-wide, not specific to fog. Fix: make the read path BOM-tolerant (decode utf-8-sig or strip in split_frontmatter) and add a round-trip test for list/show/edit/state on a BOM+CRLF task file; then drop the special-casing in edit_fog.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

