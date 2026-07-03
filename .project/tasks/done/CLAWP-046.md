---
complexity: s
created: '2026-06-05'
id: CLAWP-046
predictions:
  approach: The git-log subprocess feeding 'clawpm log commit' decodes stdout as the
    platform default (cp1252 on Windows) instead of UTF-8, so an em-dash commit subject
    is stored as mojibake (UTF-8 bytes of U+2014 read as cp1252). Find the subprocess
    reading git log and pass encoding=utf-8 with errors=replace; same foreign-input-read
    class CLAWP-045 fixed for file reads but missed in the git-log path.
  confidence: 4
  duration_min: 90
  files_changed: 2
  files_scope:
  - src/clawpm/history.py
  - src/clawpm/worklog.py
  filled_by: agent
  hypothesis: If the git-log subprocess is decoded as UTF-8, non-ASCII commit subjects
    persist correctly instead of as cp1252 mojibake.
  pre_mortem: 'Most likely failure: the decode happens in more than one path so fixing
    one leaves another mojibake source.'
  reference_tasks:
  - CLAWP-045
  success_criteria:
  - a commit with a non-ASCII subject ingested by clawpm log commit stores the exact
    UTF-8 text in work_log, asserted by a test
  - the git-log-reading subprocess uses encoding=utf-8; full suite green
priority: 5
---
# work_log git-log ingestion decodes commit subjects as cp1252 -> mojibake on non-ASCII



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

