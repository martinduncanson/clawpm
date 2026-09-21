---
baseline_ref: 06a32b7
complexity: m
created: '2026-09-02'
id: CLAWP-109
predictions:
  approach: 'Root cause (MSVCRT setargv in the exe launcher) is already tracked upstream
    and quoting cannot prevent it - not locally fixable. Mitigation instead: post-parse
    argv-count sanity check (compare len(sys.argv) against expected arg count, or
    detect a stray bare filename positional after known flags) that fails loudly with
    a specific error pointing at --scope-file / avoiding double-star in free text,
    rather than the current silent exit 0.'
  complexity: m
  confidence: 3
  duration_min: 120
  filled_by: agent
  pre_mortem: Can't detect this reliably post-hoc if the launcher already dropped
    the args before Python's argv - the mitigation may only be able to catch the SUBSET
    of cases where a known-required flag ends up missing, not silently-truncated free
    text. May end up as a docs-only fix plus --scope-file-style file-based input for
    any other free-text field prone to this.
  success_criteria:
  - A tasks add / issues add call whose CRT-mangled argv drops fields exits non-zero
    with an actionable error instead of exit 0 + no artifact
  - SKILL.md documents the double-star-in-free-text gotcha (issue tracker 2026-07-30T05:51:24Z)
    so agents avoid the literal sequence in prose, not just in glob-shaped flags
priority: 5
tags:
- windows
- silent-failure
updated: '2026-09-02'
---
# clawpm.exe silently drops any command whose args contain a double-star glob - exits 0, no artifact, no error

Issue tracker entries .agent/issues.jsonl 2026-07-07T11:13:23Z (bug/medium, launcher glob-expands via MSVCRT setargv) and 2026-07-30T05:51:24Z (observation/medium, hits ANY argument incl. free text, tasks add exits 0 with no task and no error). --scope-file (CLAWP-096-adjacent) already gives a workaround for --scope specifically, but the failure is silent for every OTHER argument too, and there's no detection/warning layer. This task is the fail-loud mitigation, not a launcher rewrite - the launcher-level cause is already tracked upstream per platform.md notes.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

