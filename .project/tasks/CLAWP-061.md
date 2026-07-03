---
baseline_ref: 84a3869
complexity: s
created: '2026-06-12'
id: CLAWP-061
predictions:
  confidence: 2
  filled_by: agent
  reference_tasks:
  - CLAWP-052
  success_criteria:
  - REVISIT only when a real cross-machine multi-writer clawpm workflow exists; then
    evaluate git-refs vs agenticq for inbox + memory-checkpoint, readable-files-primary
priority: 9
---
# h5i-style git-refs transport — FUTURE STUB (cross-machine inbox + memory/state checkpoints)

INDETERMINATE-TIME consideration — do not action without a real trigger. h5i (Apache-2.0) stores cross-agent messages, provenance, and per-agent snapshots in git refs (refs/h5i/*), union-merging across machines with NO daemon — a filesystem-first/git-native alternative to the agenticq HTTP bus. Two candidate uses IF clawpm ever goes multi-machine MULTI-WRITER: (1) cross-machine inbox transport (alternative to CLAWP-052 agenticq bridge); (2) per-agent memory/state checkpoints (consistent snapshot decoupled from the working tree, for the half-curation-pass inconsistency window a single-writer per-session commit does not cover). HARD RULE if ever adopted: readable files stay source-of-truth, refs are a DERIVED index never primary. SKIP h5i provenance-per-commit + reasoning-DAG (git layer, not clawpm PM layer). Same trigger as CLAWP-052.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

