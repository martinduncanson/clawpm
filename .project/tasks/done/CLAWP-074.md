---
baseline_ref: 0b307f2
complexity: m
created: '2026-07-03'
id: CLAWP-074
predictions:
  confidence: 4
  duration_min: 180
  filled_by: agent
  pre_mortem: 'Most likely failure: Windows runner encoding or the msvcrt cross-process
    lock subprocess tests behave differently on hosted runners; budget for one marker
    to gate runner-hostile tests'
  success_criteria:
  - Workflow runs full pytest suite on windows-latest and ubuntu-latest for Python
    3.11 and 3.12 on every PR and push to main
  - pytest-cov wired with coverage summary visible in CI output or PR comment
  - clawpm doctor --strict style gate optional but suite failure blocks merge
  - First green run recorded on the fork
priority: 3
scope:
- .github/workflows/**
- pyproject.toml
---
# CI: GitHub Actions test matrix + coverage reporting

HIGHEST-leverage structural gap (audit 2026-07-03): 1,130 tests, a Windows-encoding-centric suite, 35 merged PRs - and no .github/ directory at all. Nothing runs the tests automatically; the only gates are manual local runs + Codex/Grok review. pytest-cov is already a dev dep but never wired, so coverage of the 6,016-line cli.py is unmeasured.

SPEC: (1) .github/workflows/tests.yml - matrix windows-latest + ubuntu-latest x py3.11/3.12, uv or pip install -e ".[dev]", pytest -q. (2) Wire pytest-cov (--cov=clawpm --cov-report=term) and surface the number; a threshold gate is OPTIONAL and should start advisory. (3) Runner-hostile tests (real-subprocess msvcrt lock tests, anything needing a TTY) get a marker + skip-on-CI ONLY if they genuinely fail on hosted runners - do not blanket-skip the concurrency suite; it is the crown jewel. (4) Badge in README. NOTE: CI lands on the fork (martinduncanson/clawpm) - actions run there; courtesy upstream inherits the file harmlessly.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

