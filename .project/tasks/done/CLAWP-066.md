---
baseline_ref: 7d995ac
complexity: l
created: '2026-06-25'
id: CLAWP-066
predictions:
  confidence: 3
  duration_min: 300
  filled_by: agent
priority: 5
---
# Extend per-project lock + transient-retry to remaining task-tree mutators (edit_task, split_task direct callers, emit_tree renames)

Grok review of PR #33 (CLAWP-051) surfaced that the per-project file_lock + retry_transient hardening covers change_task_state/add_task/add_subtask but NOT all mutators: edit_task (bare tmp_path.replace + bare reload, no lock), public split_task and its direct callers (cli.py tasks split, emit_tree.py attach/promote direct renames) run outside any clawpm lock and without retry_transient. Comprehensive coverage needs a REENTRANT lock (or a lock-held param): adding file_lock inside split_task today would self-deadlock add_subtask which calls it under lock. Scope: audit every task-tree mutator, introduce reentrant-or-held locking, wrap all FS renames/reloads in retry_transient. Follow-up to CLAWP-051.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

