---
baseline_ref: 192fb54
created: '2026-10-01'
id: CLAWP-132
predictions:
  complexity: m
  confidence: 3
  duration_min: 90
  filled_by: operator
  success_criteria:
  - repro test demonstrating the team-2-a/team-2-b inference-drift squat scenario
    fails on current main
  - fix makes that repro pass without breaking the 12 CLAWP-129 tests or the 3 CLAWP-130
    tests
priority: 6
updated: '2026-10-04'
---
# CLAWP-130 residual gap: self-skip + inference drift can still squat a namespace

Codex finding on PR #68 (CLAWP-130, 2026-10-01): check_explicit_id_prefix_collision skips comparing a project against ITS OWN entry in assign_all_prefixes' assignment map (correct in general -- own_prefix, computed from explicit_prefix/_infer_prefix_from_tasks, already covers the caller's own claim for THIS create). But assign_all_prefixes' deterministic candidate for a still-taskless project can diverge from what _infer_prefix_from_tasks later infers once that project's first task is actually materialized on disk -- e.g. a taskless 'team-2-b' is predicted to mint 'TEAM-2' (distinct from taskless 'team-2-a' predicted to mint 'TEAM'); team-2-b then explicitly creates id 'TEAM-2-001' (self-entry correctly skipped, so this create succeeds); but _infer_prefix_from_tasks subsequently interprets the materialized 'TEAM-2-001.md' as prefix 'TEAM' (not 'TEAM-2'), colliding with team-2-a's own assigned 'TEAM'. team-2-a's SUBSEQUENT auto-mint then needs to extend past the now-real 'TEAM' claim and could land on 'TEAM-2', colliding with team-2-b's existing 'TEAM-2-001'. This is the SAME squat class CLAWP-130 was built to close (a sibling's eventual real state colliding with another project's existing claim), just triggered by inference-vs-candidate drift rather than a pure future-mint race. Operator decision (2026-10-01): defer rather than fix inline in CLAWP-130 -- narrow edge case (requires an id pattern where the deterministic candidate algorithm and the real post-hoc inference regex disagree on the same string), not adversarially exploitable, and sits in the same family as CLAWP-130's own two already-deferred sub-gaps (resolve_existing_prefix's dominant-prefix-only inference; the character-restricted inference regex) -- likely share a root cause in the inference-quality layer rather than being a defect specific to this check. Codex's suggested fix shape: only skip the self-entry when the incoming task will actually establish or retain that SAME predicted prefix, or compute assignments from the prospective POST-CREATE state instead of the pre-create snapshot.

## Acceptance Criteria

- [ ] No candidate yielded by `_naive_prefix_candidates` (and no `_naive_prefix_placeholder`) ends in `-<digits>`, and the chain is never empty (test: `test_no_candidate_is_ever_subtask_shaped_or_empty`).
- [ ] A subtask-shaped slice is normalised, not skipped: `_desubtask_prefix` maps `WEB-2`->`WEB2`, `TEAM-2`->`TEAM2`, `X-1-2`->`X12`, and leaves `ARB-P` and `TEAM2` unchanged (test: `test_desubtask_prefix`).
- [ ] The team-2-a / team-2-b near-twin scenario mints `TEAM-000` and `TEAM2-000`, then `TEAM2-001`; `_infer_prefix_from_tasks` re-infers `TEAM2`; a later sibling can never mint an identical literal task id (tests in `TestSubtaskShapedPrefixIsNeverAssigned`).
- [ ] A single taskless digit-suffixed project (`web-2`, `ab-2`, `x-1-2`, `q3-2026`) gets a stable prefix, no errors, and a mint/re-infer round trip (test: `test_single_taskless_digit_suffixed_project_is_assigned_and_stable`).
- [ ] Known tie, accepted loudly (operator 2026-10-04): taskless `web2` + taskless `web-2` -> `web-2` gets `WEB2`, `web2` gets the "Cannot derive a collision-free task prefix" error; an explicit `task_prefix="WEB2"` vs taskless `web-2` is refused loudly; an explicit `task_prefix` on the losing project lets it mint (`TestNormalisationCollisionIsLoudNotSilent`).
- [ ] Legacy `WEB-2-000.md` / `WEB-2-001.md` files stay resolvable via `get_task` / `list_tasks`, and the next mint is `WEB2-000` (`TestLegacySubtaskShapedPrefixFiles`).
- [ ] The `team-2-b` chain is exactly `['TEAM','TEAM2','TEAM2','TEAM-2-B']` and `_naive_prefix_reach` has size 3 (`TestCandidateChainShape`).
- [ ] The CLAWP-129 and CLAWP-130 tests still pass, and the full suite is green.
- [ ] Out of scope, documented honestly: digit-LEADING ids (`2-b`, `2024`) yield prefixes `_PREFIX_NUM_RE` (leading letter required) can never re-infer. This predates the PR; docstrings state the limit; a follow-up task is to be filed.

## Decision (b), operator 2026-10-04 [sic: given 2026-10-03]: normalise, don't skip

The literal team-2-a/team-2-b walkthrough in the writeup does not reproduce on the pre-fix code. CLAWP-130's longest-match sibling check already refuses team-2-b's explicit `TEAM-2-001` create, and `_infer_prefix_from_tasks` does not read a materialised `TEAM-2-001.md` as prefix `TEAM` (its subtask-shape filter extracts `TEAM-2` and then excludes it). The real, reproducible bug is upstream: `_naive_prefix_candidates` could hand out a `-<digits>` candidate (`TEAM-2`) that inference can never re-derive, so the project looks taskless again after its first mint and can be re-assigned a different prefix, leaving two projects with a literal `TEAM-2-000.md`. Skipping such candidates fixed that but left short ids like `web-2` with no candidate at all (`tasks add` hard-failed). Normalising (`TEAM-2` -> `TEAM2`) keeps one stable, inferable prefix and still gives every id a candidate. Cost: `web2` and `web-2` now compete for `WEB2`; the loser gets the loud error and the escape hatch is an explicit `task_prefix`.

## Notes
