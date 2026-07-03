---
complexity: m
created: '2026-05-21'
id: CLAWP-011
predictions:
  approach: 'Add Check g (cp1252-stdout-risk) to doctor: scan tracked .py files in
    each project (and clawpm itself) for non-ASCII literal characters inside print()
    / click.echo() calls, AND for open()/Path.read_text()/Path.write_text() calls
    without encoding= kwarg. Behind --check-encoding flag (similar to --check-codex;
    off by default to keep doctor offline-fast). Heuristic: regex match for print/click.echo
    lines containing non-ASCII glyphs OR file ops without encoding kwarg. Surface
    as cp1252-risk warning with file:line. Promote feedback-windows-cp1252-write-text.md
    from ''discipline rule'' to ''tooling rule'' per the memory''s own sentinel.'
  complexity: m
  confidence: 4
  duration_min: 90
  files_changed: 3
  files_scope:
  - src/clawpm/cli.py
  - src/clawpm/encoding_check.py
  - tests/test_encoding_check.py
  filled_by: agent
  hypothesis: 5 confirmed cp1252 incidents in 4 weeks is enough signal. Memory said
    '5th occurrence escalates to tooling rule'. Discipline-only enforcement keeps
    failing because new code doesn't go through a checklist that names the rule. A
    doctor check that grep-scans for the dangerous patterns catches them before they
    ship.
  pre_mortem: 'Also possible: the check itself contains a non-ASCII glyph in its source.
    Self-test the check against its own source file before declaring done.'
  reference_tasks:
  - CLAWP-008
  success_criteria:
  - doctor reports cp1252-risk warning per .py file with non-ASCII glyph inside print()/click.echo()
    line
  - doctor reports cp1252-risk warning per .py file with open()/read_text()/write_text()
    call missing encoding= kwarg
  - Off by default (--check-encoding flag); when on, runs across tracked projects'
    .py files
  - 'Test suite covers: print with → glyph (positive case), print with ASCII only
    (negative), Path.write_text without encoding (positive), open() with encoding=
    (negative)'
  - Live smoke against clawpm itself reports zero warnings (current code is clean
    per the memory's case history) OR documents any drift
  unknowns: '(1) Whether AST-based detection (parse for print/click.echo Call nodes,
    scan str args for non-ASCII) is more reliable than regex-based detection. AST
    is more correct but more code; regex is simpler. (2) Whether to also flag click.option()
    help= strings with non-ASCII (operator''s CLI surfaces these), OR only the actual
    print stream. (3) Pre-commit hook vs doctor check: doctor catches at audit time,
    pre-commit catches at write time. Both have value. Start with doctor; consider
    pre-commit as Phase 2.'
priority: 5
---
# doctor non-ASCII-in-print check: enforce ASCII-only literal glyphs in print/click.echo lines on Windows-targeting code (5th cp1252 occurrence → tooling rule per memory sentinel)



## Acceptance Criteria

- [x] doctor reports cp1252-risk warning per .py file with non-ASCII glyph inside print()/click.echo() line
- [x] doctor reports cp1252-risk warning per .py file with open()/read_text()/write_text() call missing encoding= kwarg
- [x] Off by default (--check-encoding flag); when on, runs across clawpm's own src/ + tracked projects' .py files
- [x] Test suite covers: print with arrow glyph (positive), print with ASCII only (negative), Path.write_text without encoding (positive), open() with encoding= (negative) - plus f-string, click.echo attr, bare echo Name, binary-mode, **kwargs, vendor-dir skip, self-test
- [x] Live smoke against clawpm itself reports zero warnings (fixed 5 pre-existing offences in cli.py as part of this PR: 3 em-dash echoes + 2 open() without encoding=)

## Notes

Implementation chose AST over regex per predictions.unknowns (1). Rationale: f-strings (JoinedStr) and encoding-kwarg detection are unambiguous on Call nodes but brittle by regex.

Pre-mortem requirement satisfied: `tests/test_encoding_check.py::test_encoding_check_module_is_self_clean` scans `src/clawpm/encoding_check.py` against its own check and asserts zero findings.

Did NOT extend to click.option(help=...) strings (unknowns (2)) - kept scope tight to the actual stdout/file-IO crash surface. Pre-commit hook (unknowns (3)) explicitly deferred to Phase 2.

The 5 pre-existing offences fixed in cli.py: lines 2066 (em-dash in batch dispatch echo), 2301 (open issues_file), 2451 (open worklog_path), 3431 (em-dash in scope-conflicts echo), 3449 (em-dash in conflicts list echo).

