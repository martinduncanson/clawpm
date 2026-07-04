---
baseline_ref: 0b307f2
complexity: s
created: '2026-07-03'
id: CLAWP-075
predictions:
  confidence: 3
  duration_min: 60
  filled_by: agent
  pre_mortem: 'Most likely failure: committing PM state exposes operator-internal
    notes in the public fork and courtesy upstream PRs; decide visibility BEFORE the
    first commit since history is forever'
  success_criteria:
  - A recorded decision (in this task + .project/notes/) on tracking .project/ in
    the fork, with rationale
  - 'If tracked: .project/ committed with any needed exclusions (locks already ignored)
    and visible in fork history'
  - 'If not tracked: an explicit backup mechanism for .project/ exists and is documented'
priority: 3
scope:
- .gitignore
---
# Decision: version-control .project/ PM state (gitignore contradicts doctrine)

Found by audit 2026-07-03: .gitignore line 20 excludes .project/ (added 2026-02-14 in a path-cleanup commit, no stated rationale), yet the project CLAUDE.md doctrine says per-project .project/ IS committed. Practical consequence: the ENTIRE clawpm backlog (CLAWP-068..072+), research corpus, SPEC and calibration-bearing task files exist only on this machine, unversioned and unbacked-up. The PM tool has unversioned PM state - top dogfooding contradiction.

DECISION AXES: (a) track in fork - durable, diffable, visible in courtesy upstream PRs (upstream sees our roadmap; possibly fine, possibly not); (b) keep ignored + add a backup path (e.g. the ~/.claude config-repo pattern or a private state repo); (c) track but scrub - split operator-private notes out of tracked paths. Operator call required on public visibility - do not flip unilaterally.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

