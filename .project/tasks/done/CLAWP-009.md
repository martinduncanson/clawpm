---
complexity: m
created: '2026-05-19'
depends:
- CLAWP-006
id: CLAWP-009
predictions:
  approach: 'Option A: new clawpm subcommand ''clawpm portfolio rollout-pr-agent [--repo
    X] [--all] [--dry-run]''. For each target repo: (1) verify gh auth has admin on
    it, (2) copy clawpm''s canonical .github/workflows/pr-agent.yml to a branch in
    the target repo, (3) gh secret set GEMINI_API_KEY (reading from  env or prompting),
    (4) open PR with the workflow, (5) print PR URL for operator to merge. Option
    B: standalone bash script in scripts/. Subcommand wins on discoverability (matches
    clawpm''s existing portfolio-level commands).'
  complexity: m
  confidence: 3
  duration_min: 90
  files_changed: 4
  files_scope:
  - src/clawpm/portfolio.py
  - src/clawpm/cli.py
  - scripts/rollout-pr-agent.sh
  - tests/test_portfolio_rollout.py
  filled_by: agent
  hypothesis: If a single command deploys PR-Agent across N repos in one shot, the
    friction of 'Phase 2 rollout' collapses from 30min/repo to ~3min total. Operator
    can decide go/no-go on the whole portfolio after CLAWP-006 evaluation without
    paying the per-repo deployment cost.
  pre_mortem: 'Most likely failure: variance in repo permissions (some repos operator
    owns, some are forks where they can''t set secrets). Mitigation: skip-and-report
    for repos where auth fails, don''t crash. Secondary failure: workflow file in
    target repo has different style/conventions, the canonical from clawpm clashes.
    Mitigation: keep it template-like, allow per-repo override before commit.'
  reference_tasks:
  - CLAWP-005
  success_criteria:
  - '''clawpm portfolio rollout-pr-agent --dry-run'' lists all tracked repos with
    deployment status (already-has-workflow / needs-workflow / no-gh-auth)'
  - '''clawpm portfolio rollout-pr-agent --repo X'' deploys to one repo via PR, idempotent'
  - '''clawpm portfolio rollout-pr-agent --all'' deploys to all tracked repos via
    PRs; doesn''t merge automatically (human reviews per repo)'
  - GEMINI_API_KEY pulled from env or prompted once; never echoed to stdout or written
    to disk
  unknowns: (1) Whether GH org-level secrets work for the operator OR if it must be
    per-repo. (2) Whether all tracked repos run on GH-hosted runners or some have
    specific runner labels (CT212 self-hosted runner question deferred to CLAWP-???).
    (3) How to handle repos where operator wants different PR-Agent config (e.g. different
    Gemini model variant) — global default + per-repo override?
priority: 5
---
# PR-Agent portfolio rollout: script (or clawpm CLI subcommand) to deploy workflow + secret to all clawpm-tracked repos



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

