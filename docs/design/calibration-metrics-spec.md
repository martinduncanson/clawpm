# Calibration metrics spec — clawpm reflection layer vs the self-improvement spec

**Status:** Accepted design 2026-09-02 (Fable planning session, CLAWP-104) · **Source audited:** `F:/Git/.harness-configs/distilled/self-improvement-spec.md` (2026-04-18) · **Prior review reused:** Fable design review of `q-agent/designs/v2/meta-learning.md`, session d94ce3c4 (2026-07-14), attached to COGNI-007 · **Implements via:** task tree in `.project/plans/2026-09-02-clawp104.emit.json`

This file is the **single definition** of every calibration formula clawpm computes. The gbrain bridge (cognition-layer COGNI-007 / COGNI-002) must import these definitions, not restate them, so clawpm and gbrain score identically.

## 1. Audit — what the corpus actually contains (measured 2026-09-02)

Probe: `~/clawpm/reflections/*.jsonl`, non-voided `task_done` events, latest per file.

| Fact | Value | Consequence |
|---|---|---|
| Reflection files / voided files | 611 / 186 (30%) | Void rate is high enough that "resolved" must exclude voids explicitly in every metric. |
| Non-voided `task_done` | 395 | Enough for portfolio-level metrics; per-project cells are thin (largest project 61). |
| Usable duration pairs (predicted > 0, actual > 0) | 226 | ~40/month and falling (83 May, 61 Jun, 43 Jul, 38 Aug). 30-day windows are at the noise floor; 90-day windows are meaningful. |
| Duration ratio actual/predicted quantiles | p10 0.025 · p25 0.047 · **p50 0.13** · p75 1.19 · p90 23 · p99 372 · max 977 | Bimodal. The left mode is the known "Claude says days, ships in hours" inflation. The right tail is **wall-clock contamination**: `actuals.duration_min` is first `start` → `done`, so a task parked for a fortnight scores 300x. Raw MAE on minutes is meaningless; the mean ratio (20.8) is dominated by the tail. |
| Duration hit-rate within 0.5x–2x, by confidence 1–5 | 3% · 8% · 8% · 0% (n=29/101/79/5) | **Confidence has zero discrimination on duration.** Either confidence is about something else (approach, success) or it is noise. Scoring Brier against duration would only prove predictions are always inflated. |
| Over-prediction share (ratio < 1) | 80% at conf 2–3, 63% at conf 4 | Systematic bias is large and stable: deflate. `reflect suggest` already does this; the bias metric formalises it. |
| `complexity_match` | 100% on n=150 | **Tautological.** `_compute_actuals` reads `task.complexity`, which is the prediction. This is not a measurement and must not appear in a scorecard. |
| `iterations_ratio` | 0 records | `predicted_iterations` is never set; dead axis until dispatch populates it. Report as `insufficient_data`, don't remove. |
| `surprise_taxonomy` present | 136/395; `assumption_broke` 69, `tooling_friction` 38, `complexity_misread` 20, `scope_drift` 14, `unknown_unknown` 11, `external_blocker` 7, `dependency` 6 | Tags describe the **phenomenon**. The spec's miss categories describe **why the prediction method failed**. Orthogonal; keep both. |
| `process_lesson` present | 174/395 | The L1 reflection already exists and is used. |
| Pre-registration | predictions live only in task frontmatter; JSONL first sees them at `task_done` | No tamper-evident "predicted before acting" record; `tasks edit` can rewrite predictions (CLAWP-108); closure rate is uncomputable from the ledger because unresolved predictions never enter it. |
| gbrain `takes_*` (from `garrytan/gbrain` `src/core/ops/takes.ts`) | Brier over `correct ∨ incorrect` only, probability = stated weight 0–1, `partial`/`unresolvable` excluded from Brier and tracked as `partial_rate`; calibration curve binned by weight, `bucket_size` 0.1 | These are the rules clawpm must mirror. gbrain was unreachable (CONNECT_TIMEOUT) on 2026-09-02; formulas were read from source, not the live server. |

## 2. Decisions

### 2.1 Outcome model — resolve per axis, derive one binary "held"

Metrics need a binary outcome and a probability. clawpm has neither natively, so both are **derived deterministically** (no self-grading, no model call):

| Axis | Resolution rule | Notes |
|---|---|---|
| `duration` | Let `r = active_min / predicted_duration_min` (fall back to wall-clock `duration_min` when `active_min` is unavailable; record which). `correct` if `0.5 ≤ r ≤ 2.0`; `incorrect` otherwise; `unresolvable` if either side is missing or zero. Also report `duration_debiased`: same band on `r / bucket_bias_factor` (§2.3) — tells whether the *shape* is right once the known inflation is removed. | The band is deliberately wide; the corpus hit-rate inside it is <10% today, and that is the finding, not a reason to widen further. |
| `scope` | `correct` if `files_scope` was predicted and `files_scope_overrun` is empty; `incorrect` if overrun non-empty; `unresolvable` if no scope predicted. | Already computed in `deltas`. |
| `iterations` | `correct` if `iterations_actual ≤ predicted_iterations`; else `incorrect`; `unresolvable` when either missing. | Dead today; wired for when dispatch populates it. |
| `complexity` | **Removed from scoring** until `actuals.complexity` has an independent source (`done --actual-complexity`, §2.6). `unresolvable` meanwhile. | Kills the tautology. |
| **`held`** (the Brier outcome) | `correct` if every *resolvable* axis is `correct`; `incorrect` if any resolvable axis is `incorrect`; `unresolvable` if no axis resolves. `partial` is not emitted by clawpm (there is no partial credit in a deterministic rule). | This is what confidence 1–5 is scored against. If confidence still shows no discrimination against `held`, the honest conclusion is that the field is noise and the planner should stop asking for it — that is a valid outcome of this work. |

Decision-kind tasks (`kind: decision`, ADR-2026-09-02) are bucketed separately and excluded from `duration` by default (HITL-contaminated).

### 2.2 Confidence → probability map (shared with gbrain)

| confidence | p |
|---|---|
| 1 | 0.55 |
| 2 | 0.65 |
| 3 | 0.75 |
| 4 | 0.85 |
| 5 | 0.95 |
| unset | excluded from Brier and the curve; counted in `n_unscored` |

This is the same map the COGNI-007 bridge uses as the take `weight`. Change it here or nowhere.

### 2.3 Metric set (what `reflect summarize` adds under `calibration`)

All metrics are computed over **resolved** events only (`held ∈ {correct, incorrect}`) unless stated. Every cell reports `n`; any cell with `n < 20` returns `insufficient_data: true` and null values rather than a number. Windows are by `occurred_at`.

| Metric | Formula | Reading |
|---|---|---|
| `brier` | mean over resolved of `(p − o)²`, `o = 1` if `held == correct` else `0` | 0 perfect, 0.25 = coin-flip at p=0.5. |
| `calibration_curve` | per confidence bucket: `n`, `p`, `observed = hits / n` | Same shape as gbrain `takes_calibration` (buckets by stated weight); clawpm buckets by confidence since p is a function of it. |
| `overconfidence_ratio` | per bucket `observed / p`; headline = the bucket for confidence 5 if `n ≥ 20`, else the highest bucket that qualifies | <1 overconfident, >1 underconfident. Generalises the spec's "hit-rate of 0.9-conf ÷ 0.9". |
| `mae_log` | mean of `|ln r|` over duration-resolvable events | Symmetric error in log space; 0.69 = "typically off by 2x either way". Replaces MAE-in-minutes, which the tail destroys. |
| `bias` | `median(ln r)` reported as `factor = exp(median)` and `direction` | `factor 0.13` = "predictions are 7.7x too long". Median, not mean — the tail. Per bucket (complexity, confidence, agent_profile, project) as today's `summarize` already slices. |
| `bucket_bias_factor` | the per-complexity `factor` when `n ≥ 20`, else global | Used by `duration_debiased` and already by `reflect suggest`. |
| `learning_velocity` | Brier per rolling 90-day window stepped monthly; OLS slope of Brier vs window index; reported for 90d and 365d only. 30d is **not** reported (n ≈ 40, below the gate). | Negative slope = improving. Requires ≥3 qualifying windows or `insufficient_data`. |
| `miss_category_distribution` | counts per category over the window, resolved-incorrect events only | Which root cause dominates. |
| `metareflection_due` | list of `{project_id, miss_category, count}` where the same category occurred ≥3 times in the trailing 30 days within one project | The spec's repeat rule, with the window the spec forgot. |
| `closure` | `resolved / registered` over the window, from the ledger (§2.5); plus `open_predictions` (registered, not resolved, not voided) and `void_rate` | Survivorship guard. The July review's first finding: until predictions close, every other number is fiction. Alarm text when `closure < 0.6`. |

`reflect scorecard` renders the same JSON as five lines of text: closure, bias factor, Brier + n, overconfidence headline, top miss category / metareflection due.

### 2.4 Miss-category taxonomy — first-class, single-pick, alongside surprise tags

`miss_category: Literal["wrong_signal","wrong_weight","missed_variable","operator_override","noise","structural"] | None` on `task_done` events; `clawpm done --miss-category <c>`.

- Meanings as in the source spec. clawpm gloss: `wrong_signal` = predicted on the wrong evidence (e.g. sized by file count when the cost was in review rounds); `wrong_weight` = right factors, wrong emphasis; `missed_variable` = an unmodelled factor dominated (the usual home of today's `assumption_broke`); `operator_override` = the operator changed the goal or approach mid-task; `noise` = inside expected variance, **no lesson** — a valid and encouraged answer; `structural` = clawpm/harness/tooling broke the measurement (often today's `tooling_friction`).
- Nudge, don't block: when the derived `held` is `incorrect` and no `--miss-category` was given, `done` prints a one-line reminder listing the six values. No blocking, no prompt; agent-driven closes must stay non-interactive.
- `surprise_taxonomy` stays as-is (multi-pick phenomenon tags). A guidance table maps surprise → likely miss category for the reflecting agent; it is **not** applied automatically and there is **no backfill** — legacy events carry `miss_category: null`.
- Repeat rule: computed, not stored (§2.3 `metareflection_due`). Surfaced in `reflect summarize`, `reflect scorecard`, and as a one-line Stop-hook nudge when the current project has an entry.

### 2.5 Two-line ledger — adopt forward-only, no migration

The spec's pattern (prediction line at predict time, outcome line at measure time, joined by `prediction_id`) buys two things clawpm lacks: tamper-evident pre-registration and a computable closure rate. Migration of history buys nothing (past predictions cannot be re-registered honestly).

- `predictions.prediction_id: str` (uuid4 hex) minted when predictions are first written (`tasks add`, `tasks predict`, `emit-tree`). Stored in frontmatter; carried on every reflection event for the task.
- New event `prediction_registered` appended to `~/clawpm/reflections/<task-id>.jsonl` at mint time: `{event, task_id, project_id, prediction_id, registered_at, predictions, filled_by, baseline_ref}`.
- Any later change to the predictions block appends `prediction_revised` with the new snapshot and a `reason` (free text, optional). Revisions never overwrite the registered line. `tasks edit` must go through the same path (this also forces the CLAWP-108 wholesale-replace bug to be fixed or fenced in the same PR).
- `task_done` / `task_blocked` gain `prediction_id`. For Brier, the **registered** snapshot is scored, not the latest revision, unless the revision predates the first `start` log entry (re-planning before work began is legitimate pre-registration).
- Legacy events without `prediction_id`: treated as registered at task `created`; flagged `legacy: true`; included in metrics (they are the only history there is) and counted separately in `closure`.

Cost: three write sites, one model field, one new event type, a small join in `_iter_done_events`. Benefit: closure rate becomes real, and "predicted before acting" becomes checkable.

### 2.6 Honest actuals

- `actuals.active_min`: sum over the task's work_log entries, ordered by `ts`, of `min(gap_to_next, 60)` minutes, plus 15 minutes for the final entry. Deterministic, from existing data, no schema change to the log. Reported alongside wall-clock `duration_min`; the duration axis prefers it.
- `clawpm done --actual-complexity <s|m|l|xl>`: optional independent assessment; `actuals.complexity` is **null** unless supplied. `complexity_match` is then a real signal again.

### 2.7 Pre-authorisation lanes — spec only, no implementation this round

The source spec auto-applies parameter patches inside quantified lanes. The July review's objection stands: for clawpm the "patches" are edits to CLAUDE.md, skills and planner defaults, which already sit under the L1–L4 autonomy ladder; a second authority is drift.

What a clawpm lane **would** be, when the data supports it:

- Location: `.project/constitution.yaml`, new invariant kind `lane` (constitution already validates on emission and is fail-open).
- Shape: `{name, metric, condition, window_days, min_n, action}`, e.g. `{name: duration-deflation, metric: bias.factor, condition: "< 0.5", window_days: 90, min_n: 30, action: "suggest.default_ratio := bias.factor"}`.
- Semantics in v1: `reflect summarize --lanes` evaluates conditions and emits **proposals** (JSON + an inbox message to the operator agent). It never mutates anything. Auto-apply is a separate, later decision, gated on the operator having accepted ≥N proposals of that lane by hand.
- Candidate first lanes: (a) duration deflation factor as `suggest` default (already what `suggest` computes — the lane just makes the threshold explicit); (b) planner confidence cap when `overconfidence_ratio < 0.8` over 90d with `n ≥ 30`.
- Not before: closure ≥ 0.6 for two consecutive 30-day windows and one 90-day Brier window qualifies. Both are checkable from §2.3.

### 2.8 gbrain deduplication

- Probability map (§2.2), resolution rule (§2.1), Brier definition and the "partial/unresolvable excluded" rule (§2.3) are defined here once. The COGNI-007 bridge maps `held` → take resolution (`correct`/`incorrect`/`unresolvable`), `p` → `weight`, `task_id` → take id, and must cite this file.
- Bucketing differs by design: gbrain bins by weight (0.1 steps); clawpm bins by confidence. Since p is a function of confidence, the buckets coincide at the mapped points.
- Open dependency: gbrain unreachable 2026-09-02. Nothing here waits on it; reconciliation is a cognition-layer task, cross-linked from COGNI-007's notes.

## 3. What is deliberately not done

- No nine domains, no ceremony calendar, no confidence intervals per prediction, no third ledger, no learning velocity at 30 days (all per the July review).
- No backfill of `miss_category`, no rewrite of historical events.
- No model-in-the-loop anywhere in this spec; every number is arithmetic over JSONL.
- No pre-auth auto-apply.

## 4. Success criteria for the implementation tree

1. `clawpm reflect summarize` on the real portfolio returns a `calibration` block with every metric in §2.3 present, each either a number with `n` or `insufficient_data: true`; unit tests pin the formulas against a synthetic corpus with hand-computed expected values.
2. `clawpm done --miss-category noise` persists on the event; invalid values are rejected; a miss without a category prints the nudge and still closes.
3. `tasks add` / `tasks predict` / `emit-tree` append `prediction_registered`; `tasks edit` appends `prediction_revised`; `closure` on a synthetic corpus of 10 registered / 6 resolved reads 0.6.
4. `actuals.active_min` ≤ `duration_min` on every real event, and `complexity_match` is null unless `--actual-complexity` was given.
5. This document is referenced from SKILL.md's reflection section and from COGNI-007's task notes.
