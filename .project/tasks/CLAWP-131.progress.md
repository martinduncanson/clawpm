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
updated: '2026-10-01'
---
# get_task cannot resolve a grandchild nested two directory-levels deep

Discovered while building CLAWP-128's regression test (PR #67). _candidate_task_paths (tasks.py ~220) computes only ONE level of _parent_id_of for a subtask-shaped id, then probes tasks_dir/{parent_id}/{task_id}.md -- assuming the immediate parent's directory lives at the TOP LEVEL of tasks_dir. When a directory task is itself nested under ITS OWN parent's directory (e.g. an emit-tree leaf attached under an existing parent, then further decomposed via add_subtask), the child's directory lives at tasks_dir/{grandparent}/{parent}/_task.md, not tasks_dir/{parent}/_task.md -- so a grandchild's file at tasks_dir/{grandparent}/{parent}/{grandchild}.md is never among the probed candidate paths, and get_task(grandchild_id) returns None despite the file existing on disk. change_task_state(grandchild_id, ...) then also silently returns None (it calls get_task first internally). test_nested_directory_subtask_visible_to_list_tasks (test_rollup.py) already creates this exact shape but only verifies list_tasks (a directory walk) finds the grandchild -- it never calls get_task(grandchild_id) directly, so this gap has gone unnoticed. Repro: parent=add_task(...) [top-level]; child=add_subtask(parent.id,...) [nests under parent, parent auto-splits to directory]; grandchild=add_subtask(child.id,...) [child auto-splits to directory NESTED under parent, i.e. tasks_dir/parent/child/_task.md]; get_task(grandchild.id) returns None. Fix needs _candidate_task_paths (or a helper it calls) to walk the FULL ancestor chain via repeated _parent_id_of, not just one level, when probing a nested directory-task location -- likely also affects the identical one-level-only assumption in _archive_candidate_paths.

## Acceptance Criteria

- [x] A new `_ancestor_chain(task_id)` helper walks the FULL ancestor chain
      (shallow-first) via repeated `_parent_id_of`.
- [x] `_nested_dir_candidates(task_id)` probes EVERY suffix of that chain
      with length >= 2, not just the full chain — `_parent_id_of` can't
      tell a genuine subtask relationship from a top-level id that merely
      LOOKS like one (every real clawpm id, e.g. `CLAWP-131` itself, has
      this ambiguity), so `_ancestor_chain` over-counts by exactly the
      number of spurious prefix-is-a-stem levels. An earlier version that
      joined the full chain directly passed every test using a
      hand-picked letter-suffixed top-level id (e.g. `TEST-131-A`) but
      silently failed on a REALISTIC auto-generated id (`TEST-000`) —
      caught by testing against the real auto-mint path, not just custom
      explicit ids. Probing every suffix means the genuine nesting depth
      is always among the candidates regardless of which one it is.
- [x] `_candidate_task_paths` probes the fully-nested OPEN-state directory
      path for every nesting candidate (`.md` + `.progress.md` +
      `/<task_id>/_task.md` for a further-decomposed grandchild).
- [x] `_archive_candidate_paths` gets the identical treatment (same gap,
      per this task's own repro notes).
- [x] A regression test reproduces the exact repro shape AND a realistic
      auto-generated-id variant, and asserts `get_task(grandchild_id)`
      resolves the task, not `None`.
- [x] All existing tests pass unchanged (1761+ full suite), including
      `test_nested_directory_subtask_visible_to_list_tasks` (test_rollup.py)
      and the CLAWP-085/CLAWP-128 archive-path tests.
- [x] The nested-chain probe covers all FOUR state roots (open/done/
      blocked/rejected), not just `tasks_dir` itself — a PRE-REVIEW
      subagent caught live (confidence 92) that a single ANCESTOR further
      up the chain independently transitioning (e.g. `tasks state <parent>
      blocked`, no `force` needed) relocates the grandchild's nested
      directory under that state root while the grandchild is still open,
      which the open-state-only probe missed. Fixed by iterating all four
      roots for the full chain, matching the existing one-level probe's
      own four-root pattern.
- [ ] OUT OF SCOPE (documented, not implemented): the genuinely
      combinatorial case where DIFFERENT ancestors within the same chain
      are at DIFFERENT states (not just one uniform state root for the
      whole chain, which the fix above now handles via wholesale-subtree-
      move semantics). That needs probing every ancestor-chain prefix x
      every state root independently. Deferred as a documented residual
      gap (mirrors CLAWP-130's own precedent of deferring adjacent
      sub-gaps) — not covered by this task's tests.

## Notes

Design sketch from 2026-10-01 session (traced `add_subtask`/`split_task`/
`change_task_state` to confirm how nested directory tasks move on state
transitions): the real gap is that `_candidate_task_paths`'s subtask branch
only computes ONE level via `_parent_id_of(task_id)`. A grandchild's
OPEN-state file lives at `tasks_dir/<ancestor_1>/<ancestor_2>/.../<task_id>.md`
(arbitrarily deep — `add_subtask` always creates a child inside its parent's
CURRENT directory, wherever that directory itself is nested).

