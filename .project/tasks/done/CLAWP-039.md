---
complexity: l
created: '2026-05-27'
id: CLAWP-039
predictions:
  approach: 'Borrow agenticq lease/heartbeat/expiry/fallback MODEL, implemented file-local
    over existing dispatch + CLAWP-032 locked append - no networked broker. Likely
    a doctor-style sweep detects expired leases rather than real-time, since clawpm
    has no daemon. Fallback taxonomy: requeue / route-secondary / escalate-to-human
    / fail.'
  complexity: l
  confidence: 2
  duration_min: 1440
  files_scope:
  - src/clawpm/agent.py
  - src/clawpm/dispatch.py
  - src/clawpm/cli.py
  - src/clawpm/concurrency.py
  - tests/test_dispatch.py
  filled_by: operator-edited
  hypothesis: If dispatched subtasks carry a lease with TTL + heartbeat and a fallback
    policy, a sub-agent that dies mid-task is detected and requeued/reassigned/escalated
    instead of silently stalling the parent.
  pre_mortem: File-based lease expiry needs a clock/poll mechanism clawpm lacks; if
    it requires a long-running daemon the local-first thesis breaks - constrain to
    a doctor sweep + on-dispatch check.
  predicted_iterations: 3
  reference_tasks:
  - CLAWP-037
  - CLAWP-032
  success_criteria:
  - A dispatched subtask whose holder does not heartbeat within lease TTL is detected
    (via doctor sweep or next dispatch) and transitioned per its fallback policy.
  - Lease TTL and heartbeat timestamps are recorded on the subtask and survive process
    restart (file-persisted).
  - Fallback taxonomy (requeue / route-secondary / escalate-to-human / fail) is selectable
    per dispatch and exercised by tests.
  - No long-running daemon introduced; expiry detection rides doctor + on-dispatch
    checks (local-first preserved).
priority: 6
---
# Crash-safe dispatch reassignment (lease/heartbeat/fallback, design-donored from agenticq)

Roadmap / design-donor task from the 2026-05-27 agenticq assessment. agenticq (TS/Cloudflare durable ledger) solves agent coordination at a networked layer; do NOT adopt it as infra now (wrong layer for clawpm local-first thesis). Instead harvest its lease/heartbeat/expiry/fallback model to make clawpm dispatch crash-safe over local files. Lower priority - sequence after CLAWP-037. Full integration of agenticq (as execution spine, clawpm as planning/calibration brain) is deferred until clawpm goes multi-machine/multi-harness.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

