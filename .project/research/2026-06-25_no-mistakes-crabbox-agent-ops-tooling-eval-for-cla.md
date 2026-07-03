---
created: '2026-06-25'
id: clawpm-research-no-mistakes-crabbox-agent-ops-tooling-eval-for-cla
status: open
tags:
- sprint-scouting
- backend
- cross-machine
type: investigation
---
# no-mistakes + crabbox: agent-ops tooling eval for clawpm

## Question

Do kunchenguid/no-mistakes or openclaw/crabbox offer value to adopt/adapt into clawpm? VERDICT: crabbox (remote test/exec control plane; leases throwaway cloud compute, rsyncs dirty checkout, coordinator owns lease-state + spend-caps + stale-expiry) = STRATEGICALLY VALUABLE candidate BACKEND for deferred cross-machine/off-harness dispatch (CLAWP-065/052); its lease+heartbeat+spend-cap model matures clawpm's CLAWP-039 lease design + agenticq donor pattern; spike 'clawpm tasks dispatch --runner crabbox'. no-mistakes (local git-proxy pre-push GATE: disposable worktree -> AI pipeline review/test/lint/PR -> forward-on-green; agent-native /no-mistakes) = HIGH overlap with existing codex-review+pr-review-toolkit+commit-commands+destruct-gate; borrow CONCEPTS only (gate-as-a-git-remote = un-bypassable; finding taxonomy auto-fix vs ask-user), do not adopt. Both Go/MIT/agent-native, pushed 2026-06-25.

## Summary

(To be filled in as research progresses)

## Findings

...

## Conclusion

...
