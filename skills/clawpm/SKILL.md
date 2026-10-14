---
name: clawpm
description: Multi-project task and research management (JSON-first CLI)
user-invocable: true
metadata: { "openclaw": { "homepage": "https://github.com/martinduncanson/clawpm", "upstream": "https://github.com/malphas-gh/clawpm", "requires": { "bins": ["clawpm"] }, "emoji": "📋", "install": [{ "id": "uv", "kind": "uv", "package": "git+https://github.com/martinduncanson/clawpm", "bins": ["clawpm"], "label": "Install clawpm (uv)" }] } }
---

# ClawPM Skill

Multi-project task management. All commands emit JSON by default, so JSON needs no flag. The only output-format control is the global `-f/--format [json|text]` option, which goes before the subcommand (`clawpm -f text tasks list`). There is no `--json` flag: `clawpm projects list --json` fails with `No such option: --json`.

## Reference files — load on demand

This core holds the always-on rules and the everyday commands. Everything else lives in `references/` next to this SKILL.md (`skills/clawpm/references/` in a source checkout). If this file was copied to a runtime clone root by `sync-runtime-clones.sh`, the references are at `<clone-root>/skills/clawpm/references/`. Read the file when its trigger fires; do not guess flags from memory.

| File | Load when |
|---|---|
| `references/capability-map.md` | Choosing which clawpm primitive fits a situation (decompose, conflicts, lease, mission, reflect). |
| `references/dispatch-and-scope.md` | Planning parallel dispatch (the Scope-Aware Dispatch section): `clawpm conflicts`, scope claims, glob-overlap heuristic, and the rationale/history behind the dispatch-discipline rules. |
| `references/setup-and-projects.md` | First install, `clawpm setup`, `project init`, project auto-detection, subtasks, `clawpm context`. |
| `references/command-reference.md` | You need a flag or subcommand not in Quick Start: any `tasks`, `log`, `research`, `issues`, `lease`, `mission`, `doctor` command. |
| `references/predictions-and-reflection.md` | Filling `--predict-*` / `--success-criteria` flags, completing with reflection notes, `reflect summarize`, `reflect suggest`, `reflect void`. |
| `references/messaging-and-integrations.md` | `clawpm inbox` messaging between agents, or before any commit, push, PR or merge (review/PR workflow integration points). |
| `references/multi-session.md` | Two sessions on one project, task-ID collisions, `.clawpm-tasks.lock`. |
| `references/troubleshooting.md` | A clawpm command failed or dropped an argument, `ModuleNotFoundError`, Windows glob/`--scope` problems, `add_failed`. |

The rules below stay inline on purpose: an agent that never opens a reference file must still obey them. The review and merge rules (no `/gemini review`, wait for Codex via `wait-for-codex.py`, never skip PRE-REVIEW for auth, serialization, data-storage or silently-failing-invariant changes) live only in `references/messaging-and-integrations.md`; read it before any commit, push, PR or merge.

## When to use clawpm — defacto default

clawpm is the defacto-default PM layer for every meaningful work session. Use it for any work unit that meets ANY of these:

- Produces file changes
- Takes more than ~5 minutes of focused effort
- Spans more than one logical step
- Will likely be resumed across sessions
- Has a hypothesis worth testing or a goal worth tracking
- Is dispatched to a subagent (subagent gets a subtask under the parent)

**Skip clawpm only for:** pure Q&A / explanation / brainstorming with no deliverable; single-action lookups (`git status`, "show me X"); session orientation reads; or when the operator says "just do this quickly".

If unsure, use clawpm. Under-used task entries cost nothing; lost calibration data and forgotten cross-session work compound.

**Granularity — track the work unit, not the records it processes.** clawpm tracks phases, batches, milestones, and blockers — never the individual transactions, decisions, rows, or files a task operates on. The task is "reconcile period 2026-05" or "apply approved corrections", not one task per invoice. In high-volume-record domains (accounting, data pipelines, migrations) the >5-minute default means *phases* — otherwise you file hundreds of micro-tasks.

### Two dispatch modes — pick by *how* the subagent runs

This is the steering gap most agents miss. There are two distinct ways to hand work to a subagent, and **`tasks dispatch` only fits one of them**:

- **Separate spawned `claude` process (worktree workflow):** `clawpm tasks dispatch <id> [--worktree]` writes `.claude/settings.local.json` Stop/PostToolUse/SessionStart hooks into the target dir. You then `cd` into that dir and run a **fresh `claude`** — *that* process picks up the hooks and is literally hook-gated from terminating until the rubric is satisfied (or impossibility is independently confirmed). This is the rubric-enforced contract. Use it when you genuinely spawn a separate Claude Code process per task.
- **In-harness subagent (same session, Task/Agent tool):** a subagent you dispatch from *within* your current session does **not** pick up another dir's `settings.local.json` hooks — so `tasks dispatch`'s Stop-hook enforcement does **not** apply. Instead: put structured `--success-criteria` on the task, dispatch the in-harness subagent, then verify its deliverable with the **`subagent-judge`** skill before marking done. That's the in-harness equivalent of the hook-gated rubric.

If you're driving a single Claude Code session and delegating via the Agent tool (the common case), you're in the **second** mode — reach for `success_criteria` + `subagent-judge`, not `tasks dispatch`. Reserve `tasks dispatch` for the worktree/separate-process workflow. (Rubric *scoping* discipline for either mode — never scope a dispatched rubric across a human approval gate — is in **Dispatch discipline** below.)

## How Claude fills predictions — "agent proposes, human reviews"

When adding a task on the operator's behalf, **propose all predictions in a single block, then ask for confirmation/edits.** Don't silently file with bare flags; don't ask for every field individually. Single proposal, single review beat:

> "Adding **CLAWP-099 Migrate auth to JWT** with: duration 4h (confidence 3), complexity m, approach 'drop-in JWT middleware', success criteria 'P95 <200ms; session writes drop ≥50%', pre-mortem 'mobile webview cookie edge case', reference task CLAWP-042. **Confirm or edit?**"

The operator overrides only the fields where their gut conflicts with Claude's guess. The gut-vs-Claude delta is itself calibration signal. Always include `--confidence` honestly (1 = wild guess, 5 = done-this-exact-thing-before).

## Quick Start

```bash
# From a project directory (auto-detected):
clawpm status              # See project status
clawpm next                # Get next task
clawpm start 42            # Start task (short ID works)
clawpm done 42             # Mark done

# Or set a project context:
clawpm use my-project
clawpm status              # Now uses my-project
```

## Top-Level Commands (Shortcuts)

| Command | Equivalent | Description |
|---------|------------|-------------|
| `clawpm add "Title"` | `clawpm tasks add -t "Title"` | Quick add a task |
| `clawpm add "Title" -b "desc"` | `clawpm tasks add -t "Title" -b "desc"` | Add with body |
| `clawpm add "Title" --parent 25` | - | Add subtask |
| `clawpm done 42` | `clawpm tasks state 42 done` | Mark task done |
| `clawpm start 42` | `clawpm tasks state 42 progress` | Start working |
| `clawpm block 42` | `clawpm tasks state 42 blocked` | Mark blocked |
| `clawpm unblock 42` | `clawpm tasks state 42 open` | Unblock a task |
| `clawpm next` | `clawpm projects next` | Get next task |
| `clawpm status` | - | Project overview |
| `clawpm context` | - | Full agent context |
| `clawpm use <id>` | - | Set project context |

### Don't re-`start` an in-progress task to log midway updates

**Re-starting a task that's already in-progress corrupts the duration anchor.**
Actuals are computed from the *first* start event — a re-start makes elapsed time
look shorter than it actually is, breaking the calibration signal.

Instead, use `log add --action progress` for mid-task updates:

```bash
# WRONG — resets the duration anchor
clawpm start 42

# RIGHT — logs a progress note without touching the anchor
clawpm log add --task 42 --action progress --summary "PR #125 opened, awaiting Codex review"
```

Use `start` only to transition `open` → `progress`. Use `done` / `block` / `unblock`
for the corresponding terminal transitions. Use `log add --action progress` for
everything in between.

> The CLI will warn (but not block) if you `clawpm start` a task that's already
> in-progress.

### Claude's mandate: ALWAYS predict on add

**When Claude adds a task on the operator's behalf, ALWAYS include predictions.**
Estimate at minimum: `--predict-duration`, `--predict-complexity`. Add `--hypothesis`
for any task with a non-trivial outcome ("if I do X, then Y will improve"). Add
`--predict-pitfalls` when the task touches code Claude is uncertain about. Empty
predictions defeat the reflection layer's purpose — they produce structurally-empty
events that yield no calibration signal.

For high-stakes or complex tasks, also include the Phase 1.5 fields (`--success-criteria`, `--predict-approach`, `--confidence`, `--reference-task`, `--pre-mortem`, `--unknowns`). Flag list, completion flags and reflection rules: `references/predictions-and-reflection.md`.

**Always fill in `--process-lesson` on done events — it's the rung that lets you climb.**

**Reference class > intuition.** Before predicting, look at 1-3 prior similar tasks. Pass
them via `--reference-task`. Empty `reference_tasks` = "I used the inside view only".

Attribution: `--predicted-by` exists on `clawpm tasks add`, not on the `clawpm add` shortcut. Without it, the CLI records predictions as `operator`; the MCP `tasks_add` tool records `agent`. After the operator confirms or edits your proposal, pass `--predicted-by operator-edited`. Pass `agent` only for predictions the operator has not reviewed.

## Tips

- **Flag order**: `clawpm [global flags] <command> [command flags]` — e.g. `clawpm -f text tasks list -s open`
- **JSON output**: All commands emit JSON by default; use `-f text` for human-readable
- **One command per call**: Don't chain clawpm commands with `&&` — run each separately. Don't paste `;`-separated batches either.
- **Verify batches**: after any batch of `tasks add`, run `clawpm tasks list` to confirm every task landed. On installs that predate the Windows glob fix (below), an add with a mangled argument could exit 0 and drop the task silently. The check is cheap and still worth running.
- **Portfolio root**: Default `~/clawpm`
- **Work log**: Append-only at `<portfolio>/work_log.jsonl`
- **Windows glob expansion (fixed by CLAWP-109, PR #77)**: in installs that predate that fix, Click expanded every argv entry on Windows (glob, `~`, `$VAR`, `%VAR%`), so `--scope "src/**"` arrived as file paths: a usage error when several files matched, a silently rewritten scope (`src\a.py`) when one did. Newer installs pass arguments verbatim. On 0.2.0 or older, pass patterns with `--scope-file` / `--predict-scope-file`, use a wildcard-free prefix, and keep literal double-star out of free text (`--actual`, `--context`, `--summary`; write "double-star" in prose). Those installs also silently dropped any argument containing a double-star sequence, with exit 0, so verify with `clawpm tasks list` after a batch. Options that name a filesystem path (`--target-dir`, `--body-file`, `--scope-file`, `--in-repo`, ...) still expand `~` and environment variables. Quote wildcard patterns: an unquoted one can be globbed by a POSIX shell on any OS (including Git Bash on Windows) or by PowerShell on Linux/macOS before clawpm runs. PowerShell and cmd on Windows pass arguments through verbatim.

## Dispatch discipline — rubric scoping & worktree safety

**Rule: a dispatched rubric must be satisfiable entirely within the agent's authorised action space; never scope it to an outcome that requires crossing an approval / human gate.** Phase the work so each dispatched phase terminates *at* the gate:

- **Compute phase rubric:** "divergence report for period X emitted; all candidates queued in `<ledger>`; **zero** state writes performed." The zero-writes criterion is itself gradeable from the audit log — a *prohibition* criterion, not just an achievement one.
- **Apply phase rubric:** "every decision with `status:approved` applied with an audit entry; **zero** writes lacking an approved decision ID."
- The outcome ("tie-out / books balance") is the rubric **only** for the final close task, dispatched *after* the human sign-off exists.

**Never `--worktree`-dispatch a task that mutates a single shared store.** Worktree isolation is built for parallel *code* work with disjoint file scopes (see the Scope-Aware Dispatch section in `references/dispatch-and-scope.md`). A task that appends to one shared ledger — a decisions store, `work_log.jsonl`, leases, any append-only state, **including clawpm's own JSONL stores** — must dispatch **in-place against the main repo dir**, or the ledger forks per worktree branch and records diverge or are lost. Worktree dispatch is for code; in-place dispatch is for state. `clawpm agent dispatch` is a separate path from the CLAWP-098 worktree-mutator fix: since CLAWP-115 it registers a session for its worktree only when the worktree carries its own `.project/`, and none otherwise. Read `references/dispatch-and-scope.md` before running ID-based mutators inside its worktree.
