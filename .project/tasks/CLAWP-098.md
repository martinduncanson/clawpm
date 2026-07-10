---
baseline_ref: b1575ba
created: '2026-07-06'
id: CLAWP-098
predictions:
  approach: 'Register the dispatched worktree as a temporarily-scoped project during
    its lifetime (torn down with teardown-dispatch), OR prefer cwd-resolved .project/
    over portfolio-registry lookup when cwd is inside a worktree whose id matches
    the task''s project prefix. At minimum: loud warning + documented hand-edit-and-git-mv
    workaround in dispatch output and SKILL.md until the real fix lands.'
  complexity: m
  confidence: 4
  duration_min: 240
  filled_by: agent
  pitfalls: the portfolio registry is relied upon by many other commands too (tasks
    list, next, reflect) -- any fix must not break normal single-checkout usage; the
    temporarily-scoped-project approach needs careful teardown to avoid registry bloat
    from abandoned/crashed dispatches
  success_criteria:
  - clawpm tasks state <id> done run from inside a dispatched --worktree checkout
    mutates that worktree's own task file, not the main checkout's
  - A regression test proves the current bug (state-mutator run from a worktree currently
    corrupts the main checkout) and then proves the fix
  - SKILL.md / dispatch output documents the interim hand-edit-and-git-mv workaround
    if the full fix is deferred
priority: 2
updated: '2026-07-10'
---

# Worktree-dispatched ID-mutator commands silently corrupt the MAIN checkout's task file (portfolio-registry resolution bypasses cwd)

CONFIRMED via source read (2026-07-06), reported independently by another clawpm-using session (sysops project, tracks A/B) and hit repeatedly by this session's own dispatch campaign (traced to this exact mechanism after the fact).

ROOT CAUSE: any ID-based mutator command (tasks state <id>, done <id>, start <id>, block <id>) resolves its project via get_project(config, project_id) in discovery.py, which scans config.project_roots (the GLOBAL portfolio registry, ~/clawpm/portfolio.toml) for a directory matching the task-id's project prefix. This lookup is 100% independent of cwd. When a task is dispatched via clawpm tasks dispatch <id> --worktree, the created worktree is NEVER registered as its own project -- it's just a git worktree checkout. Any ID-based state command run from inside that worktree therefore resolves via the portfolio registry back to the MAIN checkout's repo_path, and mutates the task file there.

WORKAROUND IN PRACTICE (converged on independently by two sessions): never run clawpm's state-mutating commands from inside a dispatched worktree. Hand-edit the task file's frontmatter directly and git mv it to the target state dir -- stays correctly scoped since it's a plain filesystem+git operation with no portfolio-registry involvement.

FIX MECHANISM (2026-07-10, adapted from agenticq's AgentCard identity model -- F:/Git/agenticq/src/schemas/agent-card.ts): agenticq separates a DURABLE agent_id from a per-instance session_id (UUID minted per session, multiple concurrent instances of the same agent coexist as distinct cards keyed by agent_id+session_id) and keeps all coordination state in a place every instance can see (its Durable Object ledger), never per-checkout. clawpm already has the equivalent cross-checkout-visible layer (~/clawpm/leases.jsonl, ~/clawpm/inbox/*.jsonl, ~/clawpm/dispatches.jsonl, all locked append via concurrency.append_jsonl_line) but doesn't use it for worktree/session identity. Concretely: when tasks dispatch --worktree mints a worktree, mint a session_id alongside it and register a session-scoped pointer (in the existing dispatches.jsonl, or a new portfolio-level sessions ledger) mapping session_id -> the worktree's actual filesystem path. ID-based mutator commands, when run with cwd inside a directory that matches a registered session's path, resolve against THAT session's task tree instead of falling through to the global portfolio-registry lookup. Falls back to today's registry-lookup behaviour when cwd doesn't match any registered session (preserves normal single-checkout usage).

This closes the bug WITHOUT the registry-bloat risk of registering worktrees as permanent projects (the session ledger is append-only and naturally cleaned up by teardown-dispatch, same lifecycle discipline as leases).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

