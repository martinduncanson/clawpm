# Code review style guide

Review for correctness and maintainability, not taste. Priorities, in order:

1. **Silent failures / fail-open paths (highest).** Flag any `try/except` / `catch` / fallback that swallows an error, returns a default on failure, or continues past a failed precondition without surfacing it. Security- and data-integrity-critical code must fail *closed*, not open.
2. **Correctness bugs & missed edge cases** — off-by-one, `None`/null, unhandled states, race conditions, resource leaks, unbounded retries. Cite `file:line`.
3. **Invariant & type design** — types that can represent illegal states; missing validation at trust boundaries; public APIs leaking internal invariants.
4. **Scope discipline** — flag changes that touch code unrelated to the PR's stated intent.

Do NOT flag:
- Stylistic preferences that already match the surrounding code.
- Refactors out of scope for the diff.
- "Best practice" suggestions that add complexity without a clear, stated benefit — hold to the simplest-effective-solution bar.

Conventions:
- **UK English** in user-facing strings, comments, and docs (GBP, DD/MM/YYYY).
- **Windows path-space caveat:** the dev box has a space in the user profile path (`C:\Users\Martin Workspace\`). Flag naive path handling that breaks on spaces — unquoted shell paths, string-concatenated Python paths (prefer `pathlib`/env vars).
- **Encoding:** on Windows + Python, flag file reads/writes without explicit `encoding="utf-8"`; flag foreign-input reads without `errors="replace"`.

Keep comments specific and actionable. Severity threshold: MEDIUM and above.
