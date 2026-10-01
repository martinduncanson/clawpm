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
updated: '2026-10-01'
---
# CLAWP-130 residual gap: self-skip + inference drift can still squat a namespace

Codex finding on PR #68 (CLAWP-130, 2026-10-01): check_explicit_id_prefix_collision skips comparing a project against ITS OWN entry in assign_all_prefixes' assignment map (correct in general -- own_prefix, computed from explicit_prefix/_infer_prefix_from_tasks, already covers the caller's own claim for THIS create). But assign_all_prefixes' deterministic candidate for a still-taskless project can diverge from what _infer_prefix_from_tasks later infers once that project's first task is actually materialized on disk -- e.g. a taskless 'team-2-b' is predicted to mint 'TEAM-2' (distinct from taskless 'team-2-a' predicted to mint 'TEAM'); team-2-b then explicitly creates id 'TEAM-2-001' (self-entry correctly skipped, so this create succeeds); but _infer_prefix_from_tasks subsequently interprets the materialized 'TEAM-2-001.md' as prefix 'TEAM' (not 'TEAM-2'), colliding with team-2-a's own assigned 'TEAM'. team-2-a's SUBSEQUENT auto-mint then needs to extend past the now-real 'TEAM' claim and could land on 'TEAM-2', colliding with team-2-b's existing 'TEAM-2-001'. This is the SAME squat class CLAWP-130 was built to close (a sibling's eventual real state colliding with another project's existing claim), just triggered by inference-vs-candidate drift rather than a pure future-mint race. Operator decision (2026-10-01): defer rather than fix inline in CLAWP-130 -- narrow edge case (requires an id pattern where the deterministic candidate algorithm and the real post-hoc inference regex disagree on the same string), not adversarially exploitable, and sits in the same family as CLAWP-130's own two already-deferred sub-gaps (resolve_existing_prefix's dominant-prefix-only inference; the character-restricted inference regex) -- likely share a root cause in the inference-quality layer rather than being a defect specific to this check. Codex's suggested fix shape: only skip the self-entry when the incoming task will actually establish or retain that SAME predicted prefix, or compute assignments from the prospective POST-CREATE state instead of the pre-create snapshot.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

