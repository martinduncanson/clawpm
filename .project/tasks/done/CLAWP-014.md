---
created: '2026-05-22'
id: CLAWP-014
predictions:
  approach: Walk the 41 commits. Group by feature using commit messages + diff patterns.
    For each group, draft PR title + summary + risk/conflict notes. Identify dependency
    graph (e.g. reflect Phase 1 must precede Phase 1.5). Surface items not appropriate
    to upstream. Produce ordered execution sequence with dry-run commands.
  complexity: m
  confidence: 3
  duration_min: 60
  files_changed: 1
  files_scope:
  - PR-PLAN.md
  filled_by: agent
  hypothesis: 41 commits naturally cluster into 6-8 thematic PRs (reflect subsystem,
    scope+conflicts, Windows portability, doctor checks, agent runtime adapters, restorations,
    CI). Some opt-out items (CI workflow, fork-specific URLs) should NOT be PR'd;
    some need re-framing (Phase numbering).
  pre_mortem: 'Most likely failure: upstream has accepted other PRs in the meantime
    that conflict with fork''s diff. Mitigation: plan does a fresh git fetch + dry-run
    before execution. Secondary: some commits span multiple thematic groups, hard
    to chunk cleanly. Mitigation: per-PR cherry-pick rather than range merge where
    needed.'
  reference_tasks:
  - CLAWP-006
  success_criteria:
  - 'Plan identifies 5-10 thematic PRs, each with: title, commit range, files touched,
    dependency on other PRs, upstream-ability rating (universal / opinionated / fork-only)'
  - Plan flags items NOT to upstream (e.g. fork-specific URLs in README) with rationale
  - Plan is executable at 22:00 EEST tonight without further design work — operator
    just runs the gh commands
priority: 5
---
# Upstream PR-chunking plan for malphas-gh merge

Plan and document a chunked PR strategy to merge fork's 41 commits ahead into upstream malphas-gh/clawpm. Target execution: 2026-05-22 22:00 EEST (operator's working session tonight). Output: a PR-PLAN.md or task body specifying: feature groups, per-PR scope, ordering, dependency edges, opt-out items (e.g. Windows-only fixes upstream may want as toggles, Phase numbering that may need renaming). Don't actually push or open PRs in this session — just produce the executable plan.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

