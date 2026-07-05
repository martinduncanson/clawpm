---
baseline_ref: 83a50c4
created: '2026-07-05'
id: CLAWP-096
predictions:
  complexity: s
  confidence: 3
  duration_min: 120
  filled_by: agent
  pre_mortem: Wildcard expansion may happen in the exe shim/click layer rather than
    clawpm code - if so the fix is quoting guidance plus input validation, not parsing
  success_criteria:
  - tasks add accepts --predict-scope 'scripts/**' verbatim
  - 2h30m parses to 150 minutes
  - hyphenated project ids derive single-dash alnum prefixes
priority: 3
updated: '2026-07-05'
---
# CLI ergonomics: glob-safe --predict-scope, combined duration units, prefix derivation for hyphenated project ids

From 2026-07-05 observation issue (tag cli-ergonomics), hit while batch-filing 10 tasks in code-quorum: (1) --predict-scope values with glob metacharacters (scripts/**) expand into extra positional args and fail tasks add - accept them literally (nargs/append handling or disable wildcard expansion); (2) --predict-duration rejects combined units like 2h30m - accept h+m combinations; (3) project id 'code-quorum' derives task prefix CODE-- (double dash, first-5-chars includes the hyphen) and numbering starts at 000 - strip non-alnum before prefix derivation. Repro details in F:/Git/clawpm/.agent/issues.jsonl entry 2026-07-05T10:35:32Z.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

