---
baseline_ref: fad7ec5
created: '2026-10-09'
id: CLAWP-143
priority: 5
updated: '2026-10-09'
---
# state-change move is non-atomic: shutil.move copy-then-delete failure leaves open+done copies (file and dir forms)

Codex r4 on PR #59 (CLAWP-111-001): shutil.move falls back to copy+delete on OSError; if the copy succeeds and the source delete fails, both open and done copies exist. #59's _restore_on_error restores source snapshots but not the destination. Pre-existing on main (same bare shutil.move sites in change_task_state, dir-form move, and the other callers). Fix: make the transaction track destination creation and remove it on failure (or use os.replace on same volume and treat cross-volume as copy-verify-delete), covering file form and dir form. Test with a mocked move that copies then raises.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

