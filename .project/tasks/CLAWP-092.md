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

OPERATIONAL NOTE from the external report: clawpm tasks list --flat does NOT catch a cross-branch collision (it only sees the currently-checked-out state, same blind spot as the id-allocator itself) -- the only thing that actually catches it is manually checking the specific branch you're about to file against before committing. This is a workaround, not a fix, and it is easy to forget under multi-branch pressure (exactly how the OPENW-016 near-miss happened).

Two independent hits (this campaign + an external project) raise this from "rare collision, review catches it" to "will recur whenever agentic work spans >1 unmerged branch/worktree simultaneously" -- which is now the DEFAULT shape of clawpm usage per this campaign's own experience (11+ concurrent worktrees). Recommend upgrading from observation to a real fix decision.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

