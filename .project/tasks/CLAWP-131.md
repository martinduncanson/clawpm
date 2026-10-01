---
baseline_ref: f07f502
created: '2026-09-30'
id: CLAWP-131
predictions:
  complexity: m
  confidence: 2
  duration_min: 90
  filled_by: agent
  pre_mortem: Fixing only get_task/_candidate_task_paths and missing that _archive_candidate_paths
    shares the identical one-level-only _parent_id_of assumption
  reference_tasks:
  - CLAWP-071
priority: 3
updated: '2026-09-30'
---
# get_task cannot resolve a grandchild nested two directory-levels deep

Discovered while building CLAWP-128's regression test (PR #67). _candidate_task_paths (tasks.py ~220) computes only ONE level of _parent_id_of for a subtask-shaped id, then probes tasks_dir/{parent_id}/{task_id}.md -- assuming the immediate parent's directory lives at the TOP LEVEL of tasks_dir. When a directory task is itself nested under ITS OWN parent's directory (e.g. an emit-tree leaf attached under an existing parent, then further decomposed via add_subtask), the child's directory lives at tasks_dir/{grandparent}/{parent}/_task.md, not tasks_dir/{parent}/_task.md -- so a grandchild's file at tasks_dir/{grandparent}/{parent}/{grandchild}.md is never among the probed candidate paths, and get_task(grandchild_id) returns None despite the file existing on disk. change_task_state(grandchild_id, ...) then also silently returns None (it calls get_task first internally). test_nested_directory_subtask_visible_to_list_tasks (test_rollup.py) already creates this exact shape but only verifies list_tasks (a directory walk) finds the grandchild -- it never calls get_task(grandchild_id) directly, so this gap has gone unnoticed. Repro: parent=add_task(...) [top-level]; child=add_subtask(parent.id,...) [nests under parent, parent auto-splits to directory]; grandchild=add_subtask(child.id,...) [child auto-splits to directory NESTED under parent, i.e. tasks_dir/parent/child/_task.md]; get_task(grandchild.id) returns None. Fix needs _candidate_task_paths (or a helper it calls) to walk the FULL ancestor chain via repeated _parent_id_of, not just one level, when probing a nested directory-task location -- likely also affects the identical one-level-only assumption in _archive_candidate_paths.

## Acceptance Criteria

- [ ] A new `_ancestor_chain(task_id)` helper walks the FULL ancestor chain
      (shallow-first) via repeated `_parent_id_of`, not just one level.
- [ ] `_candidate_task_paths` probes the fully-nested OPEN-state directory
      path (`tasks_dir/<ancestor_1>/.../<ancestor_n>/<task_id>.md` +
      `.progress.md` + `/<task_id>/_task.md` for a further-decomposed
      grandchild) when the ancestor chain is 2+ levels deep.
- [ ] `_archive_candidate_paths` gets the identical full-chain treatment
      (same one-level-only gap, per this task's own repro notes).
- [ ] A regression test reproduces the exact repro shape (parent ->
      add_subtask -> child [nests under parent as a directory] ->
      add_subtask -> grandchild [nests under child, which is itself nested
      under parent]) and asserts `get_task(grandchild_id)` resolves the
      task, not `None`.
- [ ] All existing tests pass unchanged, including
      `test_nested_directory_subtask_visible_to_list_tasks` (test_rollup.py)
      and the CLAWP-085/CLAWP-128 archive-path tests.
- [ ] OUT OF SCOPE (documented, not implemented): the "intermediate
      ancestor independently transitions state" combinatorial case —
      `change_task_state`'s directory-task branch always computes
      `new_dir = done_dir / task_id` (flat), so when the immediate parent
      transitions, nested children already resolve correctly via the
      existing one-level probe; when a non-immediate ancestor transitions
      instead, everything beneath it moves wholesale and keeps its
      relative nested structure, which full correctness would need to
      probe via every ancestor-chain prefix x every state root. Deferred
      as a documented residual gap (mirrors CLAWP-130's own precedent of
      deferring adjacent sub-gaps) — not covered by this task's tests.

## Notes

Design sketch from 2026-10-01 session (traced `add_subtask`/`split_task`/
`change_task_state` to confirm how nested directory tasks move on state
transitions): the real gap is that `_candidate_task_paths`'s subtask branch
only computes ONE level via `_parent_id_of(task_id)`. A grandchild's
OPEN-state file lives at `tasks_dir/<ancestor_1>/<ancestor_2>/.../<task_id>.md`
(arbitrarily deep — `add_subtask` always creates a child inside its parent's
CURRENT directory, wherever that directory itself is nested).

