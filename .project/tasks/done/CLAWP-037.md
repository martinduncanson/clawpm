---
complexity: m
created: '2026-05-27'
id: CLAWP-037
predictions:
  approach: Add force param to change_task_state to gate parent->DONE on all children
    DONE (missing child ref = unsatisfied, mirroring the cascade_unblock_dependents
    P1 fix at tasks.py:391); new 'tasks decompose' verb creates linked child task
    files each with own success_criteria via add_task; Stop-hook checks rollup deterministically
    before invoking the judge for any parent synthesis criterion; reuse parallel_group,
    no new scheduler.
  complexity: m
  confidence: 3
  duration_min: 360
  files_scope:
  - src/clawpm/tasks.py
  - src/clawpm/cli.py
  - src/clawpm/models.py
  - src/clawpm/judges/stop_condition.py
  - tests/test_rollup.py
  - tests/test_decompose.py
  filled_by: operator-edited
  hypothesis: If decomposition and per-child rubrics are recorded durably (not ephemeral
    like Kimi swarms), a parent cannot be falsely closed while children are incomplete,
    and predicted-vs-actual per subtask becomes a compounding calibration corpus.
  pre_mortem: The parent-ready rollup emit races the per-child Stop-hooks under --batch;
    mitigate by routing the work_log append and emit through the CLAWP-032 locked
    append.
  predicted_iterations: 2
  reference_tasks:
  - CLAWP-016
  - CLAWP-021
  success_criteria:
  - 'Parent with any child not DONE: ''clawpm tasks done <parent>'' exits non-zero
    and the parent file stays out of tasks/done/ (change_task_state returns None)
    - asserted by test.'
  - When all children are DONE, 'clawpm tasks done <parent>' succeeds and the parent
    file moves to tasks/done/.
  - A missing or dangling child ref is treated as UNSATISFIED (gate refuses), mirroring
    cascade_unblock_dependents missing-dep handling.
  - --force overrides the gate and writes a work_log entry naming the still-incomplete
    child IDs.
  - 'Two children completing concurrently: work_log JSONL parses cleanly and the parent-ready
    signal fires exactly once (locked append).'
  - '''clawpm tasks decompose <parent>'' creates N linked child tasks, each carrying
    its own success_criteria; parent.children reflects them.'
  - Diff introduces zero new threading/asyncio/scheduler primitives (reuses parallel_group
    + existing dispatch).
priority: 5
---
# Recorded task decomposition + per-child rubric rollup

Take Kimi K2.6 swarm decomposition (ephemeral, fan-out, latency-optimised) and make it durable: record parent->child decomposition with per-child rubrics so the parent rolls up only when all children pass. Builds entirely on existing primitives (Task.parent/children, add_task, dispatch_agent parent_id linking, evaluate_stop_condition, cascade_unblock_dependents pattern). The genuine net-new logic is the rollup GATE in change_task_state - today it is a pure file-move with no parent/child awareness. See session plan 2026-05-27.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

