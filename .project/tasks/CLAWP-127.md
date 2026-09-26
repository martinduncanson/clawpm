---
baseline_ref: 97e509e
complexity: s
created: '2026-09-26'
id: CLAWP-127
predictions:
  confidence: 4
  filled_by: agent
  hypothesis: If add_task's scan_dir list at tasks.py:1748 includes tasks_dir/rejected
    (matching done/blocked/done-archive), then a new task can never be minted with
    the same id as an already-rejected task.
  pre_mortem: 'Most likely failure: assuming the subtask-numbering path (_existing_child_ordinals)
    shares this bug without checking -- already checked (2026-09-26): it does not,
    tasks/rejected/ is already in _child_state_dirs''s scan list, so no changes needed
    there.'
  success_criteria:
  - 'New regression test: reject a task, add a new task in the same project, assert
    the new id is NOT the rejected task''s id -- confirmed failing before the fix,
    passing after'
  - Full suite passes
priority: 3
tags:
- task-id-allocation
updated: '2026-09-26'
---
# add_task's ID-numbering scan omits rejected/, can silently reuse a rejected task's id

## Live repro (2026-09-26, this exact session)

`add_task`'s ID-numbering scan (`src/clawpm/tasks.py:1748`) scans:

```python
for scan_dir in [tasks_dir, tasks_dir / "done", tasks_dir / "blocked", tasks_dir / "done" / "archive"]:
```

`tasks_dir / "rejected"` is missing from this list. CLAWP-085's own comment on this exact line says why the other three were added: *"include done/archive so an archived task's number is never re-minted... a silently reused ID would clobber archived history"* -- the same reasoning applies identically to `rejected/` (CLAWP-053's won't-do ledger), but it was never added there.

Confirmed live: `clawpm tasks add ... --project clawpm` for a new task ("updated stamp: writer stores LOCAL date...") was assigned `CLAWP-125` -- but `CLAWP-125` was ALREADY taken by a task rejected minutes earlier in the same session (`.project/tasks/rejected/CLAWP-125.md`, "Investigate TOCTOU race..."). Two files, same id, in different subdirectories:

```
.project/tasks/rejected/CLAWP-125.md   <- rejected task, id: CLAWP-125
.project/tasks/CLAWP-125.md            <- brand new task, ALSO id: CLAWP-125 (collision)
```

Caught by hand before either was committed (the new one was manually renamed to CLAWP-126 and its frontmatter `id:` field fixed to match) -- had it been committed as-is, `clawpm tasks show CLAWP-125` would have been ambiguous/wrong depending on which file glob-matched first, and any tooling keying off task id as a unique identifier (links, `--reference-task`, `supersedes`) would silently point at the wrong one.

## Fix

Add `tasks_dir / "rejected"` to the `scan_dir` list at `src/clawpm/tasks.py:1748`. One-line change, same pattern already established for `done`/`blocked`/`done/archive`.

## Test plan

- Regression test: reject a task (moving it to `rejected/`), then add a new task in the same project and assert the new task's minted id is NOT the rejected task's id (reproduces this exact collision before the fix, passes after).
- Full suite green.

## Scope note

Checked: the SUBTASK-numbering path (`_existing_child_ordinals` / `_child_state_dirs`, `tasks.py:2166-2225`) does NOT share this bug -- `_child_state_dirs`'s own docstring confirms `tasks/rejected/` is already in its scan list (CLAWP-071). Only the TOP-LEVEL `add_task` numbering scan (`tasks.py:1748`) is missing it. This is a narrow, well-isolated one-line fix plus one test; no other scan site needs touching.

## References

- Discovered while filing CLAWP-126 (PR #61 / CLAWP-123 follow-up work) in this session, 2026-09-26.
- CLAWP-085 (`.project/tasks/done/` if archived, else check history) for the original done/archive rationale this task extends to rejected/.


## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

