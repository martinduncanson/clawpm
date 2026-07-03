---
complexity: s
created: '2026-06-05'
id: CLAWP-045
predictions:
  approach: 'Assessed loose-end branch 0c59b7f: dedup already landed; glyph-swap was
    incomplete (missed help/docstring arrows the scanner can''t see). Ship the root
    fix the scanner itself recommends: guarded sys.stdout/stderr.reconfigure(utf-8,
    errors=replace) at the 3 stdout-emitting entry modules; clean residual scanner
    flags (4 em-dashes, 2 open() encoding kwargs); reincorporate dedup tests + add
    scan_path==[] regression guard.'
  confidence: 4
  duration_min: 120
  files_changed: 4
  files_scope:
  - src/clawpm/cli.py
  - src/clawpm/output.py
  - src/clawpm/judges/stop_condition.py
  - tests/test_dedup_and_encoding.py
  filled_by: agent
  hypothesis: If stdio is reconfigured to UTF-8 at entry, no output path can UnicodeEncodeError
    on cp1252 regardless of future glyphs — ending the whack-a-mole that kept reintroducing
    arrows.
  pre_mortem: 'Most likely failure: import-time reconfigure perturbs pytest/CliRunner
    output capture.'
  reference_tasks:
  - CLAWP-011
  success_criteria:
  - clawpm encoding_check.scan_path(src/clawpm) returns zero findings, pinned by a
    regression test
  - the 3 stdout-emitting modules reconfigure stdout to UTF-8; dedup tests reincorporated;
    full suite green
priority: 5
---
# Root-cause cp1252 stdout fix: reconfigure stdio to UTF-8 + scan-clean regression guard



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

