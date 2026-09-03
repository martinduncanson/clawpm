---
baseline_ref: 1f4316f
created: '2026-09-03'
id: CLAWP-115
predictions:
  complexity: m
  confidence: 3
  duration_min: 120
  filled_by: agent
  success_criteria:
  - clawpm agent dispatch registers a session for its worktree, the generated subtask
    resolves inside that worktree, and a regression test proves the eval-stop hook
    finds the task there rather than falling through to the main checkout
priority: 5
updated: '2026-09-03'
---
# CLAWP-098 follow-up: materialize the generated subtask into the agent-dispatch worktree

Codex P1 on PR #55 (thread PRRT_kwDOSVLYYc6eZw4t, agent.py). clawpm agent dispatch runs add_task, which writes the new subtask into the CALLER's checkout uncommitted, then create_worktree checks out committed HEAD - so the subtask file never lands in target_dir. PR #55 therefore deliberately does NOT register a session for that worktree (see the CLAWP-098 scope note in agent.py): registering one would point the worktree's own eval-stop hook at a .project/tasks/ that lacks the task, get_task would return None, and the Stop hook would block termination forever with 'task not found'. Not registering keeps the pre-existing behaviour, where the lookup falls through to the portfolio registry and finds the task in the main checkout. So this is a documented scope boundary, not a regression - but agent dispatch consequently gets none of CLAWP-098's worktree isolation. Proper fix: materialize the generated subtask into the worktree (copy, not commit) after create_worktree, then register the session, gated on the worktree carrying its own .project/ exactly as tasks dispatch --worktree is. Watch the interaction with the new current-revision materialization gate: an uncommitted copy is by definition not at HEAD, so the copy path needs its own contract rather than reusing that gate.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

