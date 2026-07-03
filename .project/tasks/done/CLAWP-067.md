---
baseline_ref: 01ac8ae
complexity: l
created: '2026-06-26'
id: CLAWP-067
predictions:
  confidence: 3
  duration_min: 300
  filled_by: agent
priority: 5
---
# Concurrency v3: uniform caller-contract exception handling + general 2-phase atomicity

Follow-up to CLAWP-066 (PR #34). Deferred items from the Grok/Codex review of the concurrency-v2 work: (1) UNIFORM CALLER-CONTRACT HANDLING — map the mutators' LockTimeout + new ValueErrors to clean output_error+exit(1) across all CLI/agent callers (cli.py tasks edit/split/state, cascade_unblock_dependents, agent paths); currently edit_task/split_task raise raw tracebacks on a corrupt-frontmatter or lock-timeout. (2) GENERAL 2-PHASE ATOMICITY — the task-write + parent/mission-write pairs (add_subtask child-create then parent-append; add_mission_mini_goal task-tag then mission-rewrite, currently best-effort compensation-rollback only) are serialised but not atomic; a failure between the two leaves divergent state. Needs a temp-staging / journal+rollback or two-tmp-then-commit-both pattern. (3) EXTERNAL-TAMPERING — an external (non-clawpm) delete/move between an in-lock get_task and read_text surfaces a raw FileNotFound instead of the friendly concurrent-session ValueError; wrap FS reads in the critical sections. (4) STRUCTURAL TOCTOU — change_task_state resolves task + is_directory_task classification BEFORE the lock; move inside for full snapshot consistency (currently degrades safely to a friendly FNF). (5) Optional hygiene: centralise the lock-path construction in a _project_lock_path(config, project_id) helper so the reentrancy key is canonical-by-construction at every call site.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

