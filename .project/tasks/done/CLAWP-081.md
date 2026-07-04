---
baseline_ref: 0b307f2
complexity: m
created: '2026-07-03'
id: CLAWP-081
predictions:
  confidence: 3
  duration_min: 180
  filled_by: agent
  pre_mortem: 'Most likely failure: mass fixture migration churns 40+ files in one
    diff and burns review rounds; migrate the fixture opportunistically (new tests
    + worst offenders first), not big-bang'
  success_criteria:
  - A shared portfolio fixture in conftest.py using monkeypatch.setenv replaces the
    per-file os.environ save/restore pattern (no yield-without-try/finally env restore
    remains)
  - context.py and research.py have dedicated test files (add/list/link paths)
  - doctor_apply coverage extended beyond 7 CLI-level tests to unit-level remediation
    cases
  - Full suite passes on both Python versions
priority: 5
scope:
- tests/
---
# Test hygiene: conftest fixture consolidation + thin-coverage thickening

Audit 2026-07-03 (tests): conftest.py is essentially empty (7 lines, no fixtures); every test file re-implements portfolio isolation via raw os.environ["CLAWPM_PORTFOLIO"] mutation with save/restore after yield and NO try/finally (e.g. tests/test_batch.py:49-56) - a setup failure between mutation and yield leaks env into later tests. Thin coverage: doctor_apply.py (13.8K of file-mutating remediation, 7 CLI-level tests), context.py (no dedicated tests, no context CLI invocation in suite), research.py (only indirect touches).

SPEC: (1) canonical isolated_portfolio fixture in conftest.py (tmp_path + monkeypatch.setenv); (2) migrate the leak-prone files first, others opportunistically; (3) new dedicated test files for context/research; (4) doctor_apply unit tests around the mutating remediations. Pairs with the CI task - land the fixture before coverage reporting to keep numbers honest.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

