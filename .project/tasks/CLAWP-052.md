---
complexity: m
created: '2026-06-10'
id: CLAWP-052
predictions:
  approach: 'DECISION: do NOT make agenticq a clawpm dependency. Local same-machine
    multi-session comms is solved by clawpm-native inbox (locked JSONL) + leases;
    that is the filesystem-first answer and keeps clawpm no-daemon/local. agenticq
    is for cross-MACHINE / cross-harness A2A only. When sessions genuinely span machines,
    build a THIN, env-gated, optional adapter that mirrors clawpm inbox/lease events
    to the agenticq bus (agenticq stays design-donor, never core infra). Blocked until
    a real multi-machine workflow exists.'
  confidence: 2
  duration_min: 1440
  filled_by: agent
  reference_tasks:
  - CLAWP-051
  success_criteria:
  - an env-gated optional adapter mirrors clawpm inbox/lease events to agenticq without
    adding a hard dependency (clawpm runs identically with the bridge off)
  unknowns: Whether multi-machine clawpm sessions become a real need at all - this
    task may stay parked indefinitely by design
priority: 8
---
# Optional agenticq bridge for cross-MACHINE sessions (deferred until multi-machine is real)



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

