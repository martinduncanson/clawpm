---
baseline_ref: 06a32b7
created: '2026-09-02'
id: CLAWP-110
predictions:
  confidence: 4
  duration_min: 45
  filled_by: agent
  success_criteria:
  - All 5 SKILL.md sections below are corrected and match shipped CLI behaviour; no
    code changes, docs only
priority: 5
updated: '2026-09-02'
---
# SKILL.md doc-cleanup batch (5 low-severity gaps from issues.jsonl triage)

Filed 2026-09-02 from a `.agent/issues.jsonl` triage sweep (19 entries). Five low-severity, doc-only gaps — no code changes required. Issue numbers below are 1-indexed line numbers in `.agent/issues.jsonl` as of this filing.

1. **Issue #4** — `clawpm doctor` has no `--project` flag; must run full-portfolio doctor and grep the JSON to scope to one project. Document this as the current workaround in the SKILL.md doctor section (Phase-2 ergonomic feature, not a bug to fix here — doc only).
2. **Issue #6** — `uv tool install` creates a PATH shim but doesn't register the package for `python -m` discovery. Document the canonical invocation pattern (or note the `pipx install` alternative) so a fresh agent doesn't rediscover this by trial and error.
3. **Issue #17** — `clawpm projects list --json` errors `No such option: --json`, but the skill's opening line ("All commands emit JSON by default; use -f text for human-readable output") reads as though `--json` is a valid flag. Clarify that `-f/--format` is the only formatting control and JSON needs no flag at all.
4. **Issue #18** — SKILL.md Tips section says "One command per call: Don't chain clawpm commands with &&" but doesn't warn about `;`-chained batches. Add: "After any batch of `tasks add`, run `clawpm tasks list` to confirm every task landed — a failed add may exit 0 with no output" (see `feedback_pwsh_native_arg_glob_expansion` project memory for the concrete failure mode).
5. **Issue #19 (doc portion only)** — the double-star CRT glob-expansion bug itself (silent exit 0, drops ANY argument containing `**`, not just `--scope`) is already tracked as its own code fix in **CLAWP-109** — don't duplicate that here. This item is just the cheap SKILL.md note: warn that free-text argument values (e.g. `--actual`, `--context`) must avoid literal `**` sequences on Windows, spelling it "double-star" in prose instead.

Bookkeeping note: issue #8 in the same triage (subtask `tasks add --parent` dropping predictions) was found to be stale — already fixed 2026-07-10 via CLAWP-072-006 — and has been corrected in `.agent/issues.jsonl` (`fixed: true` + resolved note) as part of this filing. No action needed on it here.

## Acceptance Criteria

- [ ] SKILL.md doctor section documents the `--project`-scoping workaround (issue #4)
- [ ] SKILL.md documents the canonical `uv tool install` invocation / discovery pattern (issue #6)
- [ ] SKILL.md clarifies `-f/--format` is the only output-format flag, `--json` doesn't exist (issue #17)
- [ ] SKILL.md Tips section adds the post-batch `tasks list` verification warning (issue #18)
- [ ] SKILL.md notes free-text arguments must avoid literal `**` on Windows (issue #19, doc portion)

## Notes

