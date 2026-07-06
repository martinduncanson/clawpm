# clawpm

Multi-project task and research management — the persistent state substrate for cross-session, cross-agent work.

## Fork-primary development model

This repo is developed primarily in the operator's fork at `github.com/martinduncanson/clawpm`. The upstream `malphas-gh/clawpm` is the original source. **The fork is canonical for our purposes** and upstream merge cadence is not a blocker on our cadence.

**Update (2026-07-06):** courtesy upstream PRs to `malphas-gh/clawpm` are no longer necessary — the upstream maintainer is now using this fork directly for his own current agentic integration work. Don't file a courtesy PR back; there's no separate upstream consumer to serve.

Operational rule:
- `fork` remote → `github.com/martinduncanson/clawpm` — where active branches and PRs live; `main` is the source of truth for local development.
- `origin` remote → `github.com/malphas-gh/clawpm` — kept for history/reference; not an active courtesy-PR target anymore per the update above.

<!-- clawpm:project-requirement -->
## Project management — clawpm

This project (clawpm) uses [clawpm](https://github.com/martinduncanson/clawpm) for task tracking and calibration capture. Any agent working in this repo MUST use the existing PM structure rather than improvising:

- `clawpm tasks list --project clawpm` — see open tasks before starting work.
- `clawpm tasks add --project clawpm --predict-*` — file new work with predictions (duration, complexity, success-criteria, pre-mortem). Use `--predicted-by agent` and ask the operator to confirm.
- `clawpm log` after substantive work; `clawpm log commit` after each commit to populate the work_log.
- `.project/SPEC.md` is the project scope; `.project/tasks/` is the live backlog; `.project/notes/` holds operator-facing notes (read these before starting).

If `clawpm` is not on PATH: `pipx install git+https://github.com/martinduncanson/clawpm` (then `clawpm doctor` to verify).
<!-- /clawpm:project-requirement -->

## Task definition discipline

clawpm ships `--success-criteria` + `emit-rubric` + the Stop-hook condition evaluator (CLAWP-016..017) specifically so tasks can be framed as **verifiable goals**, not vague intents. Use the primitive.

- "Add validation" → "Write tests for invalid inputs, then make them pass" (structured `--success-criteria` form: `{criterion, gradeable_signal, comparator}`).
- "Fix the bug" → "Write a test that reproduces it, then make it pass" — pre-state and post-state both verifiable.
- "Refactor X" → "Ensure tests pass before and after" + a measurable shape claim (LOC delta, complexity drop, etc.).

Weak criteria ("make it work", "improve X") defeat the rubric's purpose: the Stop-hook judge can't enforce what isn't measurable, and reflection events can't deliver calibration signal on success_criteria that were never gradeable in the first place. When filing a task at confidence ≥3, the rubric should already be sharp enough that another agent — or the local judge — can grade it without you in the loop.

Subagent dispatch (`clawpm tasks dispatch <id>`) puts this on the rails: the subagent literally cannot terminate until the rubric is satisfied or impossibility is independently confirmed. Verifiable goals are not just better hygiene — they're the contract.

### Canonical dispatch patterns

For recurring iteration patterns, see the dispatch playbooks under `docs/playbooks/`:

- **Codex-fix iteration loop** → `docs/playbooks/codex-fix-dispatch.md`. The canonical rubric (`wait-for-codex` clean + tests pass + PR mergeable) plus the dispatch invocation that hands the iteration to a subagent. Use this whenever a Codex review pass would otherwise consume 3-5 rounds of the parent agent's attention.

When a new recurring iteration pattern emerges (e.g. encoding-scan zero-finding loop, dependency-bump compatibility loop), capture it as a sibling playbook so the rubric + invocation are reusable.
