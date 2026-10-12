---
baseline_ref: 4bdaf22
created: '2026-10-11'
id: CLAWP-146
priority: 6
updated: '2026-10-11'
---
# BOM class: inbox and lease readers skip a BOM-prefixed first record (and settings TOML rejects BOM)

Codex r1 on PR #101 (CLAWP-144) noted, out of that diff: inbox/lease JSONL readers skip a BOM-prefixed first record and settings TOML rejects a BOM. Same class as CLAWP-144 on other file types. Audit every non-task reader, decide tolerate vs loud error per file type, add tests.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes


Also from Codex r2 on #101 (pre-existing, byte-identical at base): `edit_fog` on a file with absent frontmatter produces `---# Real task` (frontmatter terminator fused to the heading, hiding it) at tasks.py ~3537. Fix as part of this task or split. Also models.py ~204: BOM-prefixed project TOML raises TOMLDecodeError.
