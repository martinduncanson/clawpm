---
baseline_ref: 9f61ba9
created: '2026-10-07'
id: CLAWP-137
priority: 3
updated: '2026-10-09'
---
# worktree_path_for_branch: decode git output as bytes (CR/CRLF in paths altered by text=True)

Codex r2 on PR #80 (CLAWP-117): dispatch.worktree_path_for_branch uses text=True, which translates CR/CRLF even with --porcelain -z, so a valid POSIX worktree path containing a CR is reported wrongly in the branch_checked_out_elsewhere error. Capture bytes, split on NUL, decode without newline translation. Cosmetic/diagnostic only; no state is written.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

