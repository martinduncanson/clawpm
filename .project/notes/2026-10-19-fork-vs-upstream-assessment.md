# Fork vs upstream (malphas-gh/clawpm): has the fork made it better?

Date: 2026-10-19. Author: orchestrator session 587a8385. Confidence: moderate (structure and size measured; module quality and per-feature usage NOT audited line by line).

## Measured facts
- Upstream `main` = e9acf0a9 (2026-05-13), the merge of our own PR #1. Upstream has 0 commits we lack; the fork is 559 commits ahead. The upstream repo has not moved since.
- src: 4,214 LOC / 10 files (base) -> 31,816 LOC / 74 files. Tests: 3 files -> 125 files (45.9k LOC, 1.4x src). README 106 -> 1,005 lines. README command lines 29 -> 149; 34 top-level commands.
- fastapi/uvicorn moved from required to an optional `web` extra. Base had no CI, no concurrency module.
- Fork growth by subsystem (regex-grouped, approximate): agentic orchestration 12.3k LOC (39%), calibration/reflection 2.4k (7%), safety/integrity 2.2k (7%), everything else 15.0k (47%, the core grew 3.5x too).
- Commit mix since the fork point: 179 fix, 79 feat, 35 refactor, 127 chore, 57 docs. 437 commit messages mention codex/gate/P1/P2.
- Own-repo adoption (180 task files): success_criteria 142, predictions 167, baseline_ref 109, scope 41, delegability 18, stop_conditions 15.
- Reflection corpus (2026-09-02, memory): confidence 1-5 showed no discrimination against duration; complexity_match was a tautology; 30% of reflections voided.

## Verdict
BETTER: tests and CI, the concurrency/locking layer, Windows/CRLF/encoding correctness, the service-layer split of a 6k-line cli.py, the optional-dependency split, MCP surface, versioned releases.
QUESTIONABLE: the agentic orchestration layer (dispatch, judges/tournament, mission, leases, inbox, sessions) is 39% of the code with 1 test file each for tournament/judge/lease/mission and no usage artefacts found; the calibration layer, whose own corpus showed weak signal; hardening of junction and 8.3 short-name edge cases (CLAWP-159/161/162: three PRs, ~5 gate rounds) with low user-visible value.
RISK: single maintainer; 1,005-line README; each feature costs 1-5 review rounds, so the process is now a large share of the cost.

## Recommendation
Keep the core, safety layer and tests. Impose a complexity budget: no new orchestration primitives without a named user and a usage signal. Run a usage audit (command invocation counts from work_log/reflections) and mark unused orchestration modules experimental or move them behind an extra. Keep CLAWP-147-004 as small as Q1/Q2 allow.
