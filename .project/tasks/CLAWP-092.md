---
baseline_ref: 8606b04
created: '2026-07-04'
id: CLAWP-092
predictions:
  approach: Two parallel worktrees (CLAWP-078, CLAWP-079) independently computed the
    same next-free task id (CLAWP-089) when each filed a follow-up task, since separate
    worktree working directories can't see each other's uncommitted task files. Relevant
    input for CLAWP-071 (transaction integrity) scope, or a note that next-id allocation
    needs a shared-lock/counter mechanism across worktrees, not just within one checkout.
  complexity: s
  confidence: 4
  duration_min: 30
  filled_by: agent
  success_criteria:
  - Decision recorded on whether next-id allocation needs cross-worktree coordination
    (e.g. lease-style id reservation) or whether this is accepted as a rare collision
    caught by review
priority: 4
updated: '2026-07-10'
---


# Observation: cross-worktree next-task-id race (dispatch campaign 2026-07-03)

Two parallel worktrees (CLAWP-078, CLAWP-079) independently computed the same next-free task id (CLAWP-089) when each filed a follow-up task, since separate worktree working directories can't see each other's uncommitted task files.

CONFIRMED RECURRING (2026-07-10, external report from another session/project): the SAME bug class hit again, this time on a different project entirely ("OPENW" prefix) and a different manifestation -- juggling three unmerged branches in one session meant the next-id counter didn't see OPENW-016 (claimed on a sibling not-yet-merged branch, fix/whisper-language-default) and tried to reuse it. Caught and renumbered to OPENW-017 before landing, but confirms this is not a clawpm-repo-specific fluke -- it is the general shape "next-id allocation scans only what is currently visible on disk/in the checked-out branch, not what's claimed on any other not-yet-merged branch or worktree, regardless of how many separate branches happen to be juggled."

OPERATIONAL NOTE from the external report: clawpm tasks list --flat does NOT catch a cross-branch collision (it only sees the currently-checked-out state, same blind spot as the id-allocator itself) -- the only thing that actually catches it is manually checking the specific branch you're about to file against before committing. This is a workaround, not a fix, and it is easy to forget under multi-branch pressure.

FIX MECHANISM (2026-07-10, adapted from agenticq's AgentCard identity model -- F:/Git/agenticq/src/schemas/agent-card.ts, same fix family as CLAWP-098): move next-id allocation off the local filesystem scan and onto a portfolio-level, lock-protected counter -- e.g. ~/clawpm/id_counters/<project>.jsonl or a dedicated section of dispatches.jsonl, written through the SAME concurrency.append_jsonl_line primitive already used for leases/inbox/work_log. The moment any worktree or branch reserves an id (at tasks add time, not at commit time), the reservation is visible to every other concurrent worktree/branch immediately, because it lives outside any git-tracked tree -- structurally the same mechanism CLAWP-098 proposes for worktree/session identity (agenticq's agent_id:session_id pattern: keep coordination state where every instance can see it, never per-checkout). Sequence with or fold into CLAWP-098 if a session ledger gets built there -- id reservation is a natural extension of session registration, not a separate subsystem.

## Decision (2026-10-06, session 280cdc1f)

BUILD a portfolio-level id reservation ledger; do not accept the collision as "caught by review". Both incidents were caught, but only after the colliding id had already propagated into branch names, commit messages and PR titles, so renumbering is expensive. It also recurred in a second project.

Mechanism: `<portfolio_root>/id_reservations.jsonl`, append-only, written via `concurrency.append_jsonl_line`. Each record holds `{key, ordinal, task_id, project_id, ts}`. `key` is the root prefix for top-level ids and the parent id for subtask ids. The next ordinal is `max(on-disk scan, ledger high-water mark for key) + 1`. The reservation is appended inside the `portfolio_prefix_lock` that `add_task` already holds. Explicit ids are recorded too, so later auto-mints skip them.

Scope limits (accepted):
- Per-machine only. Cross-machine collisions remain, and surface loudly as a git add/add conflict on the same task filename at merge.
- Numbering gaps from abandoned branches are fine; ids are identifiers, not a dense sequence.
- An unreadable ledger, or a corrupt line in it, falls back to the scan and logs a warning. It fails open but never silently.

No overlap with CLAWP-071: that task covers atomicity within one checkout. This covers visibility across checkouts.

## Acceptance Criteria

- [ ] RED→GREEN: two separate tasks dirs (simulating two worktrees of one project) sharing one portfolio root. `add_task` in the first, then `add_task` in the second, yields distinct ids (second = first + 1), although neither dir can see the other's file.
- [ ] Same for subtasks: `add_subtask` on the same parent id in two separate tasks dirs yields distinct child ids.
- [ ] Same for `emit-tree` root ids. Its predictor and add_task's allocator must not disagree.
- [ ] An explicit `task_id` is recorded, and a later auto-mint on the same prefix skips past it.
- [ ] Ledger missing → today's behaviour, byte for byte (existing suite green). Ledger with a corrupt line → the line is skipped with a warning logged, and allocation still works.
- [ ] Full suite green in the foreground.

## Notes

