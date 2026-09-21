---
created: '2026-09-02'
id: clawpm-research-prd-prd-clawpm-calibration-metrics-and-miss-
linked_task_tree: CLAWP-112
status: open
tags:
- prd
- calibration
- reflection
- clawp-104
type: decision
---
# PRD: clawpm calibration metrics and miss-category taxonomy (CLAWP-104 audit)

## Objective
`clawpm reflect summarize` reports a real calibration scorecard (Brier, calibration curve, overconfidence, log-MAE, bias, learning velocity, miss-category distribution, closure) computed deterministically from the reflection JSONL, with formulas defined once in docs/design/calibration-metrics-spec.md and shared with the gbrain bridge.

## Why
CLAWP-104 audit. Measured 2026-09-02: 395 non-voided task_done events, 226 usable duration pairs, median actual/predicted 0.13 with a wall-clock tail to 977x, confidence 1-5 showing zero discrimination on duration, complexity_match tautological (100%), no pre-registration record, no closure rate. The existing summarize reports only duration ratios.

## Constraints
- Pure arithmetic over JSONL; no model calls; offline (gbrain unreachable).
- Forward-only ledger changes; no rewrite of historical events; no backfill of miss_category.
- Every metric cell gates on n>=20 and returns insufficient_data below it.
- Probability map and resolution rules live only in the spec; code cites the section.

## Out of scope
- Pre-authorisation lanes implementation (spec section 2.7 only).
- gbrain bridge and takes backfill (cognition-layer COGNI-007 / COGNI-002).
- Weekly operator surface cadence (weekly-memory-review integration) - a later leaf once scorecard exists.

## Success definition
Spec section 4 criteria 1-5 hold on the real portfolio and on synthetic fixtures.

## Chosen approach
Spec sections 2.1-2.6.

## Open questions
- If confidence shows no discrimination against held after M4 lands, propose retiring or redefining the field (research entry, not a leaf).

## Traceability
Ground: reflect.py, models.py, cli/reflect.py, cli/tasks.py read at fork/main 06a32b7; corpus probed with a disposable script (numbers in spec section 1); gbrain formulas read from garrytan/gbrain src/core/ops/takes.ts. UNGROUNDED-GRAPH effort tags.

