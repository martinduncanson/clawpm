---
complexity: m
created: '2026-06-05'
id: CLAWP-044
predictions:
  approach: 'New judges/tournament.py: position-bias-resistant pairwise comparison
    (run each pair in both orders; agreement = winner, disagreement = ambiguous ->
    keep higher seed). Single-elimination bracket (N-1 decided pairs) returns a winner
    + comparison log. Bracket/seeding/tie-resolution are deterministic code; only
    ''which better satisfies the rubric'' is the model call. CLI: clawpm judge tournament
    --rubric-file --candidate (repeatable). Selection feeds the existing confirm-close
    gate, does not replace it.'
  confidence: 3
  duration_min: 300
  files_changed: 4
  files_scope:
  - src/clawpm/judges/tournament.py
  - src/clawpm/judges/__init__.py
  - src/clawpm/cli.py
  - tests/test_tournament.py
  filled_by: agent
  hypothesis: If low-confidence/high-blast-radius dispatch spawns N attempts and a
    comparative judge selects the winner, output quality lifts vs single-attempt-then-verify
    (comparative judgment beats absolute scoring).
  pre_mortem: 'Most likely failure: position bias not fully cancelled by order-swap
    (model has a content-independent A-preference), so the bracket winner is seed-order-dependent
    rather than quality-dependent.'
  reference_tasks:
  - CLAWP-041
  success_criteria:
  - evaluate_tournament(rubric, candidates) returns the winning candidate via pairwise
    comparison with both-orders position-debiasing
  - deterministic tie/ambiguity resolution (disagreement on swap keeps higher seed);
    bracket is N-1 decided comparisons
  - clawpm judge tournament CLI accepts --rubric-file + repeatable --candidate files
    and emits JSON winner + comparison log
  - 'tests cover: clear winner, position-bias swap disagreement, 3+ candidate bracket,
    single candidate short-circuit; full suite green'
  unknowns: Whether single-elimination is robust enough vs round-robin for N>3; whether
    comparative prompt needs few-shot anchoring of the rubric criteria.
priority: 5
---
# Tournament judge: pairwise comparative selection among N candidate deliverables



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

