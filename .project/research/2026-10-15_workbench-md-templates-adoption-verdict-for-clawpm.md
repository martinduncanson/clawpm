---
created: '2026-10-15'
id: clawpm-research-workbench-md-templates-adoption-verdict-for-clawpm
status: open
tags:
- workbench
- adoption
type: decision
---
# workbench.md templates: adoption verdict for clawpm (2026-10-15)

## Question

Do the 8 workbench.md templates (research-mission, project-tracker, meeting-notes, agent-memory, build-loop, agent-worklog, agent-team-hq, product-spec) offer anything clawpm lacks?

## Summary

Adoption is small. Six of eight templates are already covered by clawpm primitives; one strong candidate (awaiting-human record) and one conditional (research findings schema). Source is WebFetch summaries (researcher caveat); field names must not be copied, clawpm defines its own vocabulary. Prior art for the protocol itself lives in the gbrain page workbench-md (2026-10-09), whose lifts target AgentICQ and agentic-os, not clawpm.

## Findings

- product-spec: covered by the planner PRD stage plus out_of_scope and stop_conditions fields. Rejected.
- project-tracker: covered by task states and tasks list. Rejected.
- meeting-notes: outside clawpm domain. Rejected.
- agent-memory: owned by gbrain and project memory, and the session manifest. Rejected, route away.
- build-loop done-means and verify-on-the-real-thing: covered by success_criteria, the rubric/Stop-hook judge, CLAWP-149 (merged, freshness rule) and CLAWP-153 (deterministic check, measure first). Rejected.
- agent-team-hq roles, claim, tester-only Done, worktree-per-card: covered by leases, dispatch, confirm-close and the blind refuter (CLAWP-043), and the ship-loop. Rejected.
- If-Match / 409 optimistic concurrency: clawpm uses file_lock; the open remainder is CLAWP-071 (structural TOCTOU) and CLAWP-147-004 (cross-machine ids). At most an etag note on those, no new task.
- STRONG CANDIDATE: awaiting-human / needs-you (agent-worklog, build-loop, flight-log, DECIDE: q | A | B). Today blockers are free-text (clawpm log --blocker, blocked state note) and every item held for a human lived only in chat and the manifest. Design steer: structured fields on a BLOCKED task (blocked_on human, question, options), NOT a new TaskState value (every state guard would need updating). Overlap to check first: CLAWP-022 mission mini-goals tagged agent|human, CLAWP-054 human delegability, CLAWP-111 decision-map human leaves, and the existing inbox.
- CONDITIONAL: research-mission findings (Claim, Evidence, Source, reliability tier, checked-by differs from author). File only if the Research model in src/clawpm/models.py lacks evidence and reliability structure; reuse the blind-refuter framing.
- OUT OF CLAWPM SCOPE: chief-window, appointment/exchange onboarding, managed-session supervisor, transcript-tailing adapters: routed to AgentICQ and agentic-os per the gbrain page.

## Conclusion

File one task for the awaiting-human record (propose-then-review, Martin confirms) and one umbrella rejected task so the rejections are not relitigated. Keep the research-findings schema conditional. Do not spec a task per template.
