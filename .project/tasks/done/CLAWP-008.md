---
complexity: s
created: '2026-05-19'
id: CLAWP-008
predictions:
  approach: 'Add Check f (codex-availability) to project_doctor: for each tracked
    project with a github remote (parse from repo_path''s .git/config or settings.toml),
    call gh api repos/{owner}/{repo}/issues/comments and reviews on the last 5 merged
    PRs, scan for any author login containing ''codex'' (case-insensitive, same heuristic
    the codex-review wait script uses). If zero appearances across the sample, surface
    a warning with the install URL https://github.com/settings/installations. Skip
    projects without a github remote.'
  complexity: s
  confidence: 4
  duration_min: 45
  files_changed: 3
  files_scope:
  - src/clawpm/project_doctor.py
  - src/clawpm/cli.py
  - tests/test_doctor_codex_availability.py
  filled_by: agent
  hypothesis: A clawpm doctor warning makes 'is Codex installed on this repo' a self-service
    question — operator doesn't have to manually audit installations across the portfolio
    whenever the question comes up. Catches the 'set up app at user level but selected-repositories
    only' failure mode that would otherwise stay invisible.
  pre_mortem: 'Most likely failure: heuristic produces false negatives because the
    repo''s Codex configuration triggers reviews only when @codex is tagged, not auto.
    Some PRs never get tagged → Codex never appears → false ''not installed'' warning.
    Mitigation: phrase warning as ''Codex may not be installed OR may not be configured
    to auto-review'' with remediation URL covering both. Secondary failure: github
    API rate limits across many tracked projects. Mitigation: cache results per session
    with --max-age flag.'
  reference_tasks:
  - CLAWP-000
  success_criteria:
  - doctor reports 'codex-availability' warning per project where heuristic finds
    no Codex bot in last 5 merged PRs
  - Includes remediation URL https://github.com/settings/installations in the warning
  - Projects with zero merged PRs (new projects) are skipped (no signal to act on)
  - 'Test suite covers: positive case (Codex appears), negative case (Codex absent),
    edge case (no PRs)'
  unknowns: (1) Whether the operator wants this in default doctor output or behind
    --check-codex flag. (2) Whether to detect from issue/comments OR pulls/reviews
    surface (both contain Codex output; reviews is more authoritative). (3) Whether
    the heuristic should also flag PR-Agent absence in the same check (likely yes
    — same shape of problem).
priority: 5
---
# doctor codex-availability check: warn when tracked repo has no Codex-bot appearance in recent merged PRs



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

