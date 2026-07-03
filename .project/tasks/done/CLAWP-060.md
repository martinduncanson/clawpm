---
complexity: m
created: '2026-06-12'
id: CLAWP-060
predictions:
  complexity: m
  confidence: 3
  duration_min: 240
  files_changed: 5
  filled_by: agent
  pre_mortem: most likely failure = assuming a Python-level argv guard can intercept
    it; pre-main() CRT expansion is uninterceptable from Python, so the stdin/file
    input path is the mechanism-agnostic fix that must ship regardless
  reference_tasks:
  - CLAWP-054
  - CLAWP-056
  success_criteria:
  - the expansion mechanism is confirmed by inspecting sys.argv inside clawpm for
    a --scope 'a/**' invocation on Windows (CRT/launcher globbing vs shell), and recorded
    in the task
  - glob-valued options (--scope, --predict-scope, --out-of-scope) can be set on Windows
    with the pattern stored LITERALLY (not expanded) — via a build-agnostic stdin/--scope-file
    input path and/or a launcher build fix; covered by a test passing a ** pattern
    and asserting literal storage
  - the Windows-safe filing path is documented (emit-tree JSON stdin / scope-file)
  unknowns: Exact expansion mechanism (MSVC CRT setargv in the pipx/console-script
    launcher vs PyInstaller bootloader vs shell) — determines whether a build fix
    is even possible or the stdin/file path is the only durable answer
priority: 6
---
# Windows: clawpm.exe argv glob-expands --scope / glob-valued options before Python sees them

BUG (reported from another session 2026-06-11): the native clawpm.exe on Windows glob-expands wildcard patterns in glob-valued options BEFORE Python main() sees argv — `--scope 'skill-patterns/**'` was expanded into a list of real files at the CRT/launcher level and bombed the parse (reproduced twice). PowerShell single-quoting does NOT prevent it: the expansion is below the shell, in the packaged launcher's argv handling (MSVC CRT setargv-style globbing) or equivalent.

BROADER THAN --scope: the same class hits every glob-valued option — `--scope`, `--predict-scope`, and the new `--out-of-scope` from CLAWP-054. A pure-Python argv guard CANNOT fix pre-main() expansion, so the durable fix must be mechanism-agnostic.

ALREADY-IMMUNE PATH (record, don't change): CLAWP-056 `emit-tree` takes a JSON document on STDIN — globs inside JSON never become argv tokens, so the planner->emit path is Windows-glob-safe by design. Only the manual `tasks add/edit --scope` CLI path on Windows is exposed. Current workaround: set scope via a non-shell path (emit-tree JSON stdin, or tasks edit through a path that doesn't hit the shell).

FIX (two prongs):
1. CONFIRM the mechanism first — invoke clawpm.exe with `--scope 'a/**'` on Windows and inspect `sys.argv` inside the process to see whether the pattern arrives already-expanded (CRT/launcher globbing) vs intact (shell). The right build fix depends on this (e.g. ensure the packaged launcher does NOT link argv wildcard expansion).
2. DURABLE, build-agnostic input path for glob-valued options: accept patterns via stdin or `--scope-file <path>` (one pattern per line) so the glob never appears as a shell/CRT argv token. This fixes it regardless of how the exe is packaged and aligns with clawpm's filesystem-first ethos + the 056 stdin pattern.

NOT a blocker (workaround exists; scope is optional). Low-to-moderate priority.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

