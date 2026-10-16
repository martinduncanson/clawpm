---
baseline_ref: fb0918f
complexity: m
created: '2026-10-15'
id: CLAWP-153
predictions:
  complexity: m
  confidence: 2
  duration_min: 180
  filled_by: agent
  pre_mortem: 'Most likely failure: transcripts do not carry reliable edit/test ordering
    metadata, so the measurement is noisy; mitigate by sampling and reporting uncertainty
    rather than building the check.'
  prediction_id: 4580aeb1a04442fca4775035f82d27a5
  success_criteria:
  - WHEN a sample of recorded Stop-hook transcripts is scanned THEN the share containing
    a passing test run older than the last edit is reported with n, and a build/no-build
    decision is recorded on this task
priority: 7
updated: '2026-10-16'
---
# Deterministic evidence-freshness check: last edit vs last passing test run in the Stop-hook transcript (measure prevalence first)

Follow-up from CLAWP-149 spike (PR #118): the Stop-hook transcript builder (src/clawpm/judges/stop_condition.py ~691-724) passes the raw transcript whole and does not filter by freshness; CLAWP-149 only added a prompt rule. First measure how often a Stop-hook transcript holds a passing run that predates the final edit. Only if prevalence is material, add a deterministic last-edit vs last-test-run check before the judge.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

**Decision 2026-10-17: NO-BUILD.** Measured by read-only audit worker w-153; full write-up was kept in the session scratchpad (contains local transcript ids, not committed).

- Corpus caveat: no recorded `eval-stop` transcripts exist (0 of 1,420 `stop_hook_summary` events list `clawpm eval-stop`). Proxy = every session JSONL under `~/.claude/projects/F--*clawpm*` (2026-06-05 to 2026-10-17), nothing sampled.
- Primary stratum (283 subagent runs as proxy for dispatched workers, 173 with an in-repo code edit): strict stale share 4/173 = 2.3% (Wilson 95% 0.9-5.8%); counting stale-side ambiguous runs as stale 7/173 = 4.0% (about 2.2-7.9%); no test run at all 5/173 = 2.9%; ambiguous 18/173 (10.4%, 15 lean fresh).
- Secondary stratum (main orchestrator sessions): 11/387 edit-containing Stop events stale (2.8%), all from 2 sessions.
- Threshold used: build only if the point estimate is at least 10% or the Wilson lower bound at least 5%. Both worst-case bounds fall under it, and CLAWP-149 already added a prompt rule. A deterministic check also carries false-positive cost (edit/test classification is heuristic).
- Reliability: order metadata is sound (timestamp regressions 0.03% / 0.45%; 14,430 of 14,444 tool calls pair with a result). Weak spots: background test runs, parallel calls sharing a message id, runs that end mid-work, subagent edits invisible in the parent transcript. Loose edit definitions (any shell write) give 41-46% stale, which is scratch/state-write noise, not signal.
- Not measured: done-claim detection at end of file; test-detection recall across fresh cases. Revisit only if a corpus of real `eval-stop` transcripts accumulates.

