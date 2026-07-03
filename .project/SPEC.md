# clawpm

## Overview

clawpm is a multi-project task and research management CLI — the persistent state substrate for cross-session, cross-agent software work. Filesystem-first (markdown + JSONL under `~/clawpm/` and per-repo `.project/`), no daemon, JSON-first output. On top of plain task tracking it layers:

- **Calibration**: every task is a micro-experiment — predictions (duration, complexity, approach, pre-mortem, confidence) captured at add, actuals + deltas computed at done, process lessons accumulated into a personal calibration corpus (`reflect summarize` / `suggest`).
- **Verifiable goals**: structured success-criteria rubrics (`emit-rubric`), enforced at the subagent boundary by a Stop-hook condition judge — a dispatched agent cannot terminate until the rubric is satisfied or impossibility is independently confirmed.
- **Agentic dispatch**: `tasks dispatch` / `agent dispatch` with crash-safe leases, scope-conflict pre-flight (`conflicts`), parallel batch groups, tournament comparative selection, and a filesystem inbox for inter-agent messaging.
- **Planning emission**: `emit-tree` transactional task-tree emission (consumed by the clawpm-planner skill), constitution constraints, baseline refs.

## Goals

- Durable, machine-readable PM state that survives sessions, compaction, and reboots with no daemon, no DB, no network dependency.
- Honest calibration: predictions-vs-actuals as first-class data; confidence is data; reference class > intuition.
- Verifiable task contracts: success criteria sharp enough that another agent — or the local judge — can grade them without the author in the loop.
- Safe concurrency for two-sessions-one-tree (per-project reentrant file locks, uniform mutator caller contracts, leases — CLAWP-051/066/067).
- Agent-first ergonomics: JSON everywhere, stable error codes, one-call `context`/`resume`.

## Non-Goals

- Not a daemon, service, or network bus. Cross-machine coordination stays out of core (agenticq is design-donor only — CLAWP-052; h5i git-refs transport parked — CLAWP-061).
- Not an orchestrator runtime — external orchestrators (workflows) compose by shelling the CLI, never merge in (CLAWP-065).
- Not a humans-first team issue tracker; the operator + agents are the users.
- Not a code-review tool (codex-review et al. sit alongside, wired via work_log hookpoints).

## Technical Notes

- Python 3.11+, Click CLI, `src/clawpm/` (~30 modules), ~1,130 pytest tests.
- Task files: `.project/tasks/*.md`, YAML frontmatter; state encoded by directory (`tasks/`, `done/`, `blocked/`) + `.progress.md` suffix; work log append-only JSONL; reflections per-task JSONL under `~/clawpm/reflections/`.
- Fork-primary development: `martinduncanson/clawpm` (fork remote) is canonical; `malphas-gh/clawpm` (origin) receives courtesy upstream PRs.
- Windows is a first-class platform: UTF-8 stdio reconfigure (CLAWP-045/046), msvcrt cross-process locks, spaced-profile-path discipline.

## Reflection

(Reserved — populate at milestone reviews per the project-level reflection loop.)
