# PROPOSAL (not applied): CLAUDE.md task-discipline text for the calibration layer

Date: 2026-10-19. Task: CLAWP-163 part 2 (closes the last unmet 112-006 criterion on CLAUDE.md). Status: WAITING FOR OPERATOR AGREEMENT. Nothing in `CLAUDE.md` has been edited; the auto-mode classifier blocks CLAUDE.md edits, so this must not be retried by an agent. Martin applies it, or says "go" and a session applies it by the route he names.

## Scope
Add one subsection, `### Closing a task honestly (calibration)`, under `## Task definition discipline` in `F:/Git/clawpm/CLAUDE.md` (the project file). About 8 lines. No other file; no hooks; no global CLAUDE.md.

## Proposed text
```
### Closing a task honestly (calibration)

- Close with `clawpm tasks state <id> done` and, when the outcome differed from the prediction, add `--miss-category <wrong_signal|wrong_weight|missed_variable|operator_override|noise|structural>` (pick one; `noise` is a valid answer), `--actual-complexity <s|m|l|xl>` and `--resolution "<one line>"`. `done` nudges (never blocks) when a missed prediction has no category.
- Read the scorecard with `clawpm reflect scorecard`; every cell with n<20 reports insufficient data, so do not tune predictions from it. Definitions: `docs/design/calibration-metrics-spec.md` (2.1-2.4).
- Use `clawpm tasks list --frontier <root-id>` to see what is startable under a decision map.
```

## Justification
CLAWP-112-006 names CLAUDE.md in its scope; the flags it lists exist today (`--miss-category`, `--actual-complexity`, `--resolution` in `cli/tasks.py` and `cli/shortcuts.py`, `reflect scorecard` in `cli/reflect.py`, `--frontier` in `cli/tasks.py`). Without the text, agents working in this repo close tasks without categories and the miss taxonomy stays empty. SKILL.md already carries the long form; CLAUDE.md only needs the pointer.

## Security gate / inviolable touched
Core-config policy (`CLAUDE.md` is core config): proposal, operator agreement, then commit/PR at L2. No constitution item is touched.

## Rollback
`git revert` of the single commit; the text is additive and nothing reads it programmatically.

## Honest doubt
The fork-vs-upstream assessment (2026-10-19) found the calibration layer is the weakest-signal subsystem (confidence showed no discrimination in the 2026-09 corpus). Adding agent-facing instructions makes agents do more ceremony for that layer. If Martin would rather not, the alternative is to close CLAWP-112 by amending criterion 112-006 to drop the CLAUDE.md clause. My recommendation: apply the 3-bullet text above (small, additive) and keep the layer under the complexity budget.
