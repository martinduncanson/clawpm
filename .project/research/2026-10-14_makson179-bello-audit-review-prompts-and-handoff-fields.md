---
created: '2026-10-14'
id: clawpm-research-makson179-bello-audit-review-prompts-and-handoff-fields
status: open
tags:
- prior-art
- ship-loop
- review-gates
- dispatch
type: investigation
---
# Makson179/Bello audit: review prompts, evidence floor, handoff fields

## Question

Does Makson179/Bello contain anything worth adapting into clawpm config and workflows (ship-loop playbook, worker briefs, code-quorum style review gates, dispatch/lease/inbox design, CLAUDE.md conventions)?

## Summary

Narrow yes, and almost all of it is prompt text, not code. Bello is a single-run Python supervisor around ONE coder agent (Codex or Claude Code) in a disposable workspace. It adds a runtime supervisor, a completion reviewer, an adversary and a local log distiller. It has no task ledger, no multi-task orchestration, no lease table and no inbox, so the dispatch/lease/inbox half of the question is "not applicable". The value is in its reviewer and adversary prompts in `supervisor/prompts/prompts.toml` and in a few structural habits around them. Licence is MIT (GitHub API `license.spdx_id` = MIT, `LICENSE` file present), so ideas and short code or prompt excerpts may be reused with the copyright notice. A CLA exists, but it binds only contributions to Bello. Bello's headline benchmark numbers are vendor-run (3 runs per cell, ProgramBench) and trade time for usage (Efficient Budget: 66.3% less weekly Codex limit, but 1:49:44 mean runtime against 0:28:27). Treat them as marketing, not evidence for our design.

## Findings (all verified by reading the file named)

- `prompts.toml` `completion_review` step 4 sorts every issue into required behavior / discretionary refinement / cosmetic preference, and only required behavior may block accept. Our ship-loop stop rule only says "P3 only: merge". Candidate: a three-class rule in the gate brief.
- Same step: "return everything at once", then a completeness check: "if the coder fixed exactly the defects in validation_gaps and changed nothing else, would you accept? If not, your list is incomplete." This targets the drip-feed pattern in our memory `codex_review_trajectory` (same bug class across rounds). Cheap to paste into the review brief.
- `behavior_surface` is a list the reviewer rebuilds and returns whole each round; entries are never dropped, only marked `out_of_scope` with a reason; the next round sweeps the whole list, not just the changed area. Plus `prior_uncovered_edge_candidates`: the previous reviewer's unverified suspicions are handed to the next one as the first things to check. This is a durable cross-round carry-forward. We have nothing equivalent between gate rounds (each worker round starts from findings only).
- Restart handoff schema (`handoff`): `{objective, restart_reason, bad_pattern, known_evidence, next_step, recovery_signal}`, "self-contained, no patch or test code". Our ship-loop section 1 says "dispatch a FRESH worker from the report" but gives no brief template for a failed-round redispatch. `bad_pattern` and `recovery_signal` are the useful fields.
- Restart criterion: "restart only when the coder is not converging (repeats a mistake after steering, reintroduces fixed defects, ignores findings); not merely because N returns have occurred." Matches our existing stop rule ("findings per round not falling"). Already have it.
- Adversary prompt: receives the finished result without development history, runs in a disposable snapshot, findings only on "ground 1" (mechanically observed failure with exact command and raw output); anything else is an "observation". Our blind refuter (CLAWP-043) covers the blind-reviewer half. The reproduction-required finding standard is the part we do not have in writing.
- `adv_report_controller` prompt: a second model adjudicates the adversary's findings (keep / reject / downgrade-to-observation) but must copy kept findings and all observations VERBATIM and add no content of its own. The controller then adds fixed category definitions. Design point: a filter that can delete or downgrade but cannot rewrite, so the coder never sees a laundered paraphrase. Relevant to code-quorum multi-lens merges, not to clawpm core.
- `completion_context.py`: reviewer evidence is written once per wake to read-only files (chmod 0o400/0o500), listed in an index with short rows, and `assert_unchanged()` re-hashes every file (sha256) before the next write. Prompt carries objective plus index, not history. Our `clawpm pr verify` (CLAWP-147-001) already attests head sha; the file-level hash freeze is stronger than we need.
- Evidence floor in the completion prompt: accept needs "a fresh passing behavioral validation after the last relevant edit"; static/lint/build checks are "hygiene, never behavioral evidence"; a changed or coder-written test is NOT a defect by itself. Deterministic side in `controller_parts/command_parsing.py` `_validation_from_action`: a behavioral run counts as pass only if tests actually executed (`_tests_executed`), and a parsed `failed_count > 0` overrides exit code 0. Freshness is `validation.sequence > latest_relevant_change_sequence` (`completion_context.py:_evidence_summary`).
- CAVEAT found while verifying: the same function says "without reviving the removed shell-shape/masking classifier" and sets `masking_reason=None` unconditionally. The prompts and `evidence_rules.py` still describe `masked_or_unknown` flags that current `main` never produces in this path. The prompt text overstates the machinery. Do not copy the "masked" vocabulary.
- clawpm side, by reading `src/clawpm/judges/stop_condition.py`: the Stop-hook judge is transcript-only LLM grading; the confirm-close refuter prompt looks for "tests pass but no test run in transcript" but its text says nothing about a run that predates the last edit. I did not read the transcript builder, so whether freshness is checked elsewhere is not verified.
- `bello-delegate/SKILL.md`: launcher/observer rules worth matching. Durable run state lives in files (`PROGRESS.md`, `DECISIONS.md`, `FINAL_REPORT.md`); process liveness is "corroboration, not proof"; reject a duplicate active launch; on failure report the diagnostic and do not silently start a replacement run; do not edit the delegate's state files. We already have leases, worktree-per-worker and "orchestrator re-dispatches explicitly", so this is mostly confirmation.
- `docs/crash-recovery.md`: durable restart budget (max 3 per logical run, 1/2/4 s), restart only when the guardian proves the old process tree is dead AND the checkpoint has no uncertain action; a command that finished after the checkpoint but was unobserved is not adopted as evidence; an interrupted patch application is never replayed. Process-level, daemon-shaped; clawpm's spec says no daemon. Skip, but the "uncertain action blocks automatic continuation" principle matches our crash-safe leases.
- Coder prompts "deliberately never mention hidden or grading material, naming it primes gaming" (header of `prompts.toml`). Opposite of our verifiable-goals stance (workers see the rubric). Different threat model (benchmark integrity). Skip.
- Review budgets: `max_completion_returns_before_adversary` / `_after_adversary`, integer or `"unlimited"`. We have a qualitative stop rule only, no numeric round cap that forces escalation to the operator.

## Ranked table

| # | Item | Verdict | Effort | Evidence |
|---|---|---|---|---|
| 1 | Three-class finding rule (required / discretionary / cosmetic; only required blocks) plus "return everything at once" completeness check, in the gate brief and ship-loop stop rule | Adopt (genuine gap: stop rule is P3-only) | S, docs/brief text | `prompts.toml` `completion_review` step 4 |
| 2 | Carry-forward ledger between gate rounds: running behaviour-surface list (never drop, mark out_of_scope) plus previous round's unverified suspicions as the next round's first checks | Adapt (needs a file in the PR branch or task spec; code-quorum owns the reviewer side) | M | `prompts.toml` 2a/3, `prior_uncovered_edge_candidates` in `completion_context.py` |
| 3 | Failed-round redispatch brief template with `bad_pattern`, `recovery_signal`, `known_evidence`, `next_step`, no patch | Adopt (gap: ship-loop gives no template) | S, add to `ship-loop/worker-rules.md` or playbook | `prompts.toml` handoff schema line 239 |
| 4 | Evidence-freshness rule in the rubric: a gradeable signal only counts if it was produced after the last edit; static checks are not behavioural evidence | Adapt (add to `_grading_instructions` and the refuter EVIDENCE lens; deterministic transcript check is a separate, larger task) | S for prompt, M for deterministic check | `completion_inputs`, `_evidence_summary`; clawpm `rubric.py:_grading_instructions`, `stop_condition.py` refuter prompt |
| 5 | Finding standard for blind review: a finding needs exact command/input plus raw observed output; else it is an observation that does not block | Adapt (code-quorum adversarial lens, not clawpm core) | S | adversary prompt `<what_counts_as_a_real_defect>`, `<discipline>` |
| 6 | Verbatim-forwarding filter (adjudicator may keep/reject/downgrade, never rewrite) | Adapt, route to code-quorum | M | `adv_report_controller` prompt |
| 7 | Numeric gate-round budget per PR, then escalate to operator instead of another worker | Adapt (judgment call; our qualitative stop rule may already be enough) | S | `review_limits.py` |
| 8 | Delegate launcher rules (files are truth, liveness is corroboration, no silent replacement) | Already do (leases, ship-loop section 1) | none | `bello-delegate/SKILL.md` |
| 9 | Immutable hashed evidence files for reviewer | Skip (CLAWP-147-001 head attestation covers the real risk) | L | `completion_context.py` |
| 10 | Crash recovery, watchdog, process fencing, snapshot patch-back transactions | Not applicable (daemon/sandbox shaped; spec non-goal) | n/a | `docs/crash-recovery.md`, `architecture.md` |
| 11 | Cheap-model triage router (noop/escalate), log distiller (ModernBERT), Smart Execution, model-mix config advisor | Not applicable (cost features of a single-coder runtime; distiller is a 599 MB model, Apache-2.0 separate) | n/a | README, `prompts.toml` `cheap_runtime` |
| 12 | Masking classifier vocabulary | Skip (removed in current code, see caveat) | n/a | `command_parsing.py` ~line 575-599 |

## Proposed follow-up tasks (one-liners)

1. Ship-loop: add the required / discretionary / cosmetic rule and the "fixed exactly these, would I accept?" completeness check to section 5 Stop rule and to the Codex gate brief template.
2. Ship-loop: add a failed-round redispatch brief template (objective, bad_pattern, known_evidence, next_step, recovery_signal, no patch code) to `worker-rules.md` or the playbook section 1.
3. Rubric: add a freshness line to `_grading_instructions` and the refuter EVIDENCE lens ("a check only counts if it ran after the last relevant edit; lint/build/type checks are hygiene"), with a test on the rendered text.
4. Spike (S): measure how often a Stop-hook transcript contains a passing test run that predates the final edit; only then decide on a deterministic freshness check.
5. Code-quorum (route to that project): carry-forward behaviour-surface ledger between rounds, reproduction-required finding standard, verbatim adjudicator filter.
6. Decide (operator): numeric gate-round cap per PR, default and escalation path.

## Not verified

- Bello's benchmark claims (ProgramBench scores, 66.3% usage saving); numbers read from README only, not reproduced, small n.
- `approval_triage.py`, `adversary_agent.py`, `watchdog.py`, `immutable_reads.py`, `snapshot_*`, `appserver.py`, the Pi worker, `bello-config-advisor` references and the native Codex build scripts were downloaded or listed but not read. Claims about the adversary workspace and the report-controller pipeline come from the prompts and architecture docs, not from the controller code.
- Whether the completion reviewer prompt actually reduces round count: no data in the repo.
- clawpm inbox semantics and the transcript builder feeding the Stop-hook judge were NOT read; the freshness gap (item 4) is inferred from the judge and refuter prompt text only.
- DeepWiki was not consulted; the repo was read through the GitHub contents API. The GitHub MCP plugin was unavailable (auth header error); `gh` worked.
- Repo is young (created 2026-05-24, 143 stars, last push 2026-10-08); churn risk is high, so pin to the content read today rather than a moving link.
