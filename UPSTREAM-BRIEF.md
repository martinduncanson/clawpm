# clawpm fork → upstream — change brief

> Fork: `martinduncanson/clawpm` (active dev). Upstream: `malphas-gh/clawpm`. 41 commits ahead, 35 files changed, +8,753 / -187 LOC. Drafted 2026-05-22.

## What — features added

- **Phase 1: reflect subsystem** (`reflect.py` +219 / `tasks.py` +106). Predictions block on task add (`--predict-duration`, `--predict-complexity`, `--predict-scope`, `--hypothesis`). Actuals captured on close. Deltas computed. JSONL reflection log per task. Why: turn the PM layer into a calibration substrate, not just a tracker.
- **Phase 1.5: applied-science predictions + recursive meta-reflection**. `Predictions.filled_by` field (`agent` / `operator` / `derived`). Meta-reflection — reflections on reflections. Why: separate gut-vs-Claude calibration signal; agent-proposed predictions must be tagged distinct from human ones.
- **Phase 1.6: doctor checks + reflect void**. `clawpm doctor` checks stale-task, prefix-collision, drift-between-spec-and-code. `reflect-void` surfaces tasks closed without a reflection. Why: forcing function for calibration hygiene.
- **Phase 1.7: inbox** (`inbox.py` +286). Inter-agent messaging — one agent leaves structured notes for another, scoped per project or global. Why: subagent dispatch + cross-session handoff needs a persistence layer; ephemeral TodoWrite-style state evaporates.
- **Phase 1.8: project announce + dogfood checks**. `clawpm projects announce` writes a marker block into project's CLAUDE.md/AGENTS.md/README.md ("this project is clawpm-tracked"). `doctor` adds `commit-drift` (commits since last `work_log` entry) and `missing-marker` checks. Why: make clawpm self-evident to any agent that opens the repo, and detect when operator/agent forgets to `log commit`.
- **Reflection-uptake v1** (6 fixes, `test_reflection_uptake.py` +554). Closes feedback loop — `clawpm reflections list` surfaces patterns; doctor flags repeated failure modes. Why: capture without uptake = noise.
- **Scope field + `clawpm conflicts`**. Per-task `scope` glob field (`--scope "src/foo/**"`). New `clawpm conflicts` command finds tasks with overlapping scope. Why: detect concurrent work collisions before they happen; pre-flight check before dispatching subagents.
- **Issues: observation type + `--tag` flag**. `clawpm issues add --type observation --tag <label>` for non-blocking signal (e.g. L3-decision logging, depth-warning, calibration deltas). Why: separate "must fix" from "worth noting" in the issue stream.
- **CLAWP-008: `doctor --check-codex`** (`codex_check.py` +257 / `test_codex_check.py` +446). Walks last 5 closed PRs per project, scans for `chatgpt-codex-connector[bot]` activity; warns if Codex app isn't installed/active on a project. Why: reviewer-triangle (Claude writer + Codex + PR-Agent) requires Codex to actually be wired up; silent absence is the failure mode.
- **CLAWP-011: `doctor --check-encoding`** (`encoding_check.py` +240, in flight on PR #5). AST-scans `.py` for cp1252-risk patterns: non-ASCII in `print/click.echo`, missing `encoding=` on `open/read_text/write_text`, modules with print but no `sys.stdout.reconfigure`. Why: 6 confirmed Windows UnicodeEncodeError incidents in 5 weeks; sentinel tripped, discipline-rule escalated to tooling-rule.

## What — Windows / portability fixes

- **TOML backslash silent swallow** — `repo_path = "F:\Git\..."` parses without error but backslash-eats next char. Fix: forward slashes mandated; `find_project_dir_fallback` recovers from malformed repo_path by walking up looking for `.project/settings.toml`. Why: cost a full debug session before being identified.
- **cp1252 encoding family** — `encoding="utf-8"` everywhere for own-source reads/writes; `read_bytes() + decode(errors="replace")` for foreign-source reads (e.g. third-party markdown); structured `unreadable_files` warning when reads fail (surfaces path so operator can clean up). ASCII-only in hand-written `click.echo`/`print`. Why: same family as TOML-backslash — Windows default behaviour silently breaks.
- **ID collision after split** — running `clawpm projects split` could orphan task IDs by re-numbering. Fix: ID generation now stable under split. Why: data loss bug.
- **Project dedup in discovery** — sibling clones / worktrees with same `id` (e.g. `arb-prd`, `arb-prd-pr-sprint`) appear once, preferring canonical directory (name == id). Why: `projects list` previously double-counted.

## What — agent runtime adapters

- **`CLAUDE.md` at repo root** — instructs Claude Code agents how to use clawpm in this repo. (Self-dogfooded.)
- **`AGENTS.md.template` + `codex-instructions.md`** — drop-in template for Codex / agents-spec runtimes. Codex-specific bootstrap & tool-descriptor work explicitly TODO'd ("the team that owns the runtime owns the adapter").
- **`skills/clawpm-cowork/SKILL.md`** — bootstrap skill for ephemeral Cowork-style sandboxes: clones operator's portfolio repo, `uv tool install clawpm`, runs `clawpm context`.
- **`sync-runtime-clones.sh`** — keeps the skill-loader-path mirror in sync (`~/.claude/skills/clawpm/skills/clawpm/SKILL.md`) when the canonical lives elsewhere.

## What — CI / review workflow

- **`.github/workflows/pr-agent.yml`** — PR-Agent (the-pr-agent/pr-agent) on Gemini Flash, free-tier API. Calibration data so far: 0 unique bugs caught on a 5-PR sample (Codex+PRE-REVIEW caught 6). Gated decision pending. Why: cost-free second reviewer experiment; provisional.
- **PRE-REVIEW discipline** (Claude Code subagent dispatch before `git push`). Documented in `SKILL.md` workflow grid. Why: Codex round-trip is ~3min; cheaper to catch obvious bugs locally first.
- **README quickstart rewrite** — install-first ordering, copy-paste-ready commands.

## How — architectural decisions worth flagging upstream

- **JSON-first by default** (`-f text` for human-readable). All commands emit machine-parseable JSON. Why: agents are the primary consumer; humans secondary. Reverses the default vs most CLI tools.
- **Filesystem-first state** — `~/clawpm/` portfolio.toml + per-project `.project/`. No DB, no daemon, no migrations. Why: survives crashes, version-controllable per-project, debuggable with `cat`.
- **Predictions are first-class** — every task carries duration / complexity / scope / hypothesis predictions. The fork's calibration loop assumes this; the PM layer becomes a measurement surface. Why: gut-vs-actual delta is the highest-signal data we can capture per task.
- **`scope` glob over freeform** — machine-comparable. `clawpm conflicts` can detect overlap without LLM call. Why: deterministic-first (model for judgment, code for facts).
- **Dogfooding stance** — clawpm uses itself for its own development (Phase 1.8). Why: stress-tests the tooling against real adversarial workflow.

## Why this isn't all upstream-ready

- **Operator-personalised conventions** baked in places (e.g. ASCII-only echo, propose-then-review pattern, Windows-first portability assumptions). Upstream may want different defaults.
- **Skill files** (`skills/clawpm/SKILL.md`, `skills/clawpm-cowork/SKILL.md`) are Claude Code runtime-coupled and won't load in vanilla environments without the harness.
- **Phase numbering** (1, 1.5, 1.6, 1.7, 1.8) reflects fork's evolution; upstream may want a flat versioning scheme.
- **Some checks are subjective** (e.g. ASCII-only echo discipline) and may not belong in core; could be doctor plugins.

Happy to selectively upstream — the Phase 1 reflect subsystem + Windows portability fixes + scope/conflicts are the most universally-applicable; the dogfood + announce + codex-check pieces are more opinionated.
