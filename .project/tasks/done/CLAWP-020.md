---
created: '2026-05-22'
id: CLAWP-020
predictions:
  approach: Hook into existing state-transition path in clawpm/commands/state.py done().
    After writing the done file, scan all blocked/ + open tasks; for each, re-evaluate
    depends_on; if all deps now done, move (if blocked) and emit work_log event 'cascade_unblock'
    citing the trigger task. Doctor check iterates blocked/ checking mtime + depends_on
    resolution.
  complexity: s
  confidence: 4
  duration_min: 120
  files_scope:
  - clawpm/commands/state.py
  - clawpm/commands/doctor.py
  - tests/test_cascade.py
  filled_by: agent
  pitfalls: Cycle detection in depends_on graph — clawpm currently doesn't enforce
    DAG; cascade could loop on a malformed graph. Need cycle check + abort with warning
  pre_mortem: 'Most likely failure: a depends_on cycle (A blocks B blocks A) triggers
    infinite cascade. Mitigation: visited-set in cascade traversal; doctor check for
    cycles in depends_on graph.'
  success_criteria:
  - Completing a parent task auto-promotes all blocked children whose deps are now
    satisfied
  - cascade_unblock entries appear in work_log.jsonl with from_task and to_task fields
  - clawpm doctor flags blocked tasks with all-deps-done as stale-blocked
  - No regression in existing state-transition tests
priority: 5
---
# Dependency cascade auto-unblock on task done

On any task state -> done, scan sibling tasks where depends_on includes the completed ID. If all deps now done, transition blocked -> open (or keep open if already open) and emit a cascade_unblock work_log event. Add clawpm doctor check 'stale-blocked': task in blocked/ with no remaining open deps for >24h -> warning. Cheap, deterministic, immediately useful — removes a manual step operators forget. Lifted from guild's findNewlyUnblocked() + malphas/ralph's task-next-respects-DAG + task-magic's deps-as-gate.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

