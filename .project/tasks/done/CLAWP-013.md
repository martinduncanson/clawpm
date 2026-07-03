---
created: '2026-05-22'
id: CLAWP-013
predictions:
  approach: Read current README. Identify gaps vs restorations + this-session features
    (encoding check, AGENTS.md). Edit in-place. Run a quick scan for broken paths/links
    via grep.
  complexity: s
  confidence: 4
  duration_min: 30
  files_changed: 2
  files_scope:
  - README.md,**/README.md
  filled_by: agent
  hypothesis: README currently does not mention history-import command, clawpm-sync
    hook, or examples/portfolio fixtures. After restoration, these need quickstart-level
    discoverability.
  pre_mortem: 'Most likely failure: README has its own quirky structure I disrupt
    by adding sections in the wrong place. Read full file first; match existing voice/flow.'
  success_criteria:
  - README mentions clawpm reflect history-import command + clawpm-sync hook install
    path + examples/portfolio quickstart
  - All in-repo cross-references resolve (no broken paths)
  - repo-hygiene clean (no obvious dead files, missing docstrings on new public APIs)
priority: 5
---
# README + repo-hygiene pass after restorations

After the 3 restoration commits (examples/portfolio, history-import, clawpm-sync hook), update README to reference the restored features. Run repo-hygiene skill if available, else manual: check for stale references, broken intra-repo links, undocumented public APIs, leftover scaffolding. Verify .gitignore changes didn't accidentally ignore something needed. Also check if AGENTS.md / UPSTREAM-BRIEF.md (added this session) need README cross-references.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

