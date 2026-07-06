---
baseline_ref: 01ac8ae
complexity: l
created: '2026-06-26'
id: CLAWP-071
predictions:
  success_criteria:
  - '2-phase add_subtask: a build/staging failure of the parent children-list update
    leaves NEITHER child file NOR parent modified; a failure after child-commit but
    before parent-append leaves only an orphan child that parent_rollup_status still
    counts via its dir-scan backstop and whose subtask number is not reused (fault-injection
    test)'
  - '2-phase mission: add_mission_mini_goal validates the mission is renderable BEFORE
    writing the task frontmatter (missing/unparseable mission leaves task un-tagged,
    no divergence); a failure during the mission write rolls the task frontmatter
    back (fault-injection tests for both pre-write and post-write failure points)'
  - 'structural TOCTOU: change_task_state resolves the task and classifies is_directory_task
    INSIDE the per-project lock (single consistent snapshot), verified by a test that
    the classification happens under the lock'
  - 'external-tamper wraps: an externally-deleted file mid-critical-section in edit_task,
    add_mission_mini_goal, serve response-append, and _write_rejection_frontmatter
    raises a friendly ConcurrentModificationError(ValueError) with an actionable message,
    not a raw FileNotFoundError (test per site)'
  - full test suite green including all new fault-injection/concurrency tests; Codex
    + Grok review clean
priority: 5
updated: '2026-07-06'
---

# Concurrency v4: transaction integrity (2-phase atomicity + structural TOCTOU + external-tampering)

Follow-up to CLAWP-067 (which delivered the uniform CLI caller-contract exception mapping). The deeper transaction-integrity items, deferred as higher-risk restructures:
(1) GENERAL 2-PHASE ATOMICITY — task-write + parent/mission-write pairs (add_subtask child-create then parent-append; add_mission_mini_goal task-tag then mission-rewrite, currently best-effort compensation-rollback only) are serialised but not atomic. Needs temp-staging / journal+rollback or two-tmp-then-commit-both so a failure between the writes can't leave divergent state.
(2) STRUCTURAL TOCTOU — change_task_state resolves the task + is_directory_task classification BEFORE the lock; move both inside for full snapshot consistency (currently degrades safely to a friendly FileNotFoundError, so this is hardening not a live bug).
(3) EXTERNAL-TAMPERING FS WRAPS — an external (non-clawpm) delete/move between an in-lock get_task and read_text surfaces a raw FileNotFound instead of the friendly concurrent-session ValueError; wrap the read_text/replace bodies in the critical sections (edit_task, mission, serve, _write_rejection_frontmatter) to map FS errors to actionable ValueErrors before leaving the lock.
NOTE: the lock-path-helper hygiene item from CLAWP-066/067 review is now MOOT — file_lock's isabs enforcement (CLAWP-066) already makes the reentrancy key canonical-by-construction.
Source: Grok/Codex review threads on PR #34 + #35.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

