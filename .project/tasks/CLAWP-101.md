---
baseline_ref: 85edc30
created: '2026-07-07'
id: CLAWP-101
predictions:
  approach: Additive payload:dict field on inbox send_message events + a materialize
    command that turns pending task_request messages into real tasks.add calls from
    the receiving agent's OWN safe context -- single-writer discipline preserved throughout,
    a1 never writes .project/tasks/ directly.
  complexity: m
  confidence: 3
  duration_min: 180
  filled_by: agent
  pitfalls: must not weaken the single-writer property for any convenience reason;
    --dry-run needs to be genuinely side-effect-free like emit-tree's; decide inbox
    materialize vs tasks add --from-inbox shape before implementing, don't build both
  success_criteria:
  - send_message accepts an optional structured payload; existing plain-text messages
    are unaffected (schema is additive)
  - A new command materializes pending task_request inbox messages into real tasks
    via the receiving agent's own tasks add path, acks them, and replies with the
    new task id via in_reply_to
  - A test proves a1's inbox send + a2's materialize round-trip produces a correctly-scoped
    task with zero direct .project/ writes from a1
  - --dry-run previews without writing
priority: 6
updated: '2026-07-07'
---
# Structured inbox payload + inbox materialize: safe cross-session task handoff

Design (2026-07-07, sidebar discussion): enable a1 (an external agent session) to hand off work to a2 (a session actively iterating through a clawpm backlog) WITHOUT any risk of clobbering a2's in-flight state -- single-writer discipline, not shared writes.

ROOT INSIGHT (verified against source): `~/clawpm/inbox/<agent-id>.jsonl` already lives at the PORTFOLIO ROOT, entirely outside any project's git-tracked `.project/` tree -- append-only, cross-platform locked writes (`concurrency.append_jsonl_line`). This is structurally immune to every cross-session collision class hit during the 2026-07-03..07 dispatch campaign (git add -A sweeping stray files, cross-worktree next-ID races CLAWP-089/090/092, CLAWP-098's portfolio-registry-bypasses-cwd bug). A mechanism built on the inbox never touches a2's live git/task state at all.

THE GAP: `send_message(portfolio_root, to, message, from_agent, in_reply_to, project, task)` (inbox.py:91) only carries a free-text `message` string plus optional `project`/`task` tags -- no structured payload a2 could mechanically turn into a real task. Today a1's request is prose a2's orchestrating agent has to manually read and interpret before running `tasks add` itself.

SPEC:
1. Add an optional `payload: dict | None` field to the inbox message event schema (`send_message` + the JSONL event shape in inbox.py). Additive-only -- existing readers ignore the new key, no migration needed, no behaviour change for plain-text messages.
2. CLI: `clawpm inbox send --to <agent> --message "..." --task-request` (or a repeatable set of flags mirroring `tasks add`'s predict-* surface: `--title`, `--predict-duration`, `--predict-complexity`, `--priority`, `--success-criteria`, etc.) that builds the structured payload and sets `payload.type = "task_request"`.
3. New `clawpm inbox materialize [--agent-id <id>] [--dry-run]` (or `tasks add --from-inbox <msg-id>` as an alternative shape -- pick whichever composes better with the CLAWP-077 service layer) that:
   - Reads pending (unacked) messages for the given agent-id via the existing `read_inbox`.
   - For each with `payload.type == "task_request"`, calls `tasks add` (or the CLAWP-077 service-layer `tasks` add function once it exists) using the payload fields, from a2's OWN safe execution context.
   - Acks the materialized message (`ack_messages`, already exists) and replies with the new task's id via `in_reply_to` (already supported), so a1 can track what happened to its request.
   - `--dry-run` previews what would be created without writing anything (mirrors `emit-tree --dry-run`'s pattern).
4. a1 NEVER writes into `.project/tasks/` directly at any point in this flow -- only a2 does, via its own already-safe `tasks add` path. That single-writer property is the entire safety mechanism; don't weaken it by adding any path where a1 writes task files itself.
5. No daemon, no polling loop -- a2 drains its inbox at natural checkpoints (session start, between subagent dispatches, on heartbeat), matching clawpm's existing lazy-sweep philosophy (same pattern as lease expiry sweeping).

OUT OF SCOPE: real-time interrupt/notification (a1 can't force a2 to check RIGHT NOW -- that's a daemon, which clawpm deliberately doesn't have); a1 directly writing task files under any circumstance; a shared/multi-writer .project/tasks/proposed/ staging directory (considered and rejected -- still inside the git-tracked project tree, reintroduces the exact collision classes the portfolio-level inbox avoids).

SEQUENCING: ship after the current 2026-07-03..07 campaign wraps (CLAWP-077/068/076 and the rest). Low urgency, real value once multiple concurrent clawpm sessions/agents become routine (which this campaign's own experience argues they already are).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

