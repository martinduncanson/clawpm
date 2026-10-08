# Changelog

All notable changes to clawpm are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/).

Every merged feature or fix PR adds one line under `## [Unreleased]`. See "Release discipline" in `CLAUDE.md`. The file-move log is `archive/CHANGELOG.md` (separate, unrelated to releases).

## [Unreleased]

### Added

- `tasks dispatch --max-iterations N`: an absolute cap on Stop-hook rubric iterations, counted from a per-dispatch baseline; at the cap `hook eval-stop` stops the agent with a `MAX_ITERATIONS` triage message, and fails closed (Stop allowed, enforcement-error message) if the iteration log cannot be read or written. `clawpm loop` was evaluated and folded into dispatch (`docs/design/loop.md`); `--max-budget` deferred (#87, CLAWP-070)
- Doc-staleness gate (`tests/test_clawp102_readme_staleness.py`, run by the existing CI pytest step): fails when a non-hidden command or long option in `clawpm introspect` has no mention in README's `## All commands` section; README gaps it found (mission/inbox subcommands, `introspect`, `project announce`, `research link`, ~70 flags) are now documented (#85, CLAWP-102).
- `tasks dispatch --worktree` and `agent dispatch` now support a project that lives in a repository subdirectory: the session record carries an optional repo-relative `project_prefix` (absent for root-level projects, so existing records resolve unchanged), the agent runs in the project root inside the worktree, and session-scoped resolution maps any cwd under the worktree to `<worktree>/<prefix>/.project`; the `monorepo_worktree_unsupported` refusal is removed. `agent dispatch` fails closed (nothing created) when it cannot determine the project's repo prefix, and a session prefix containing control characters is rejected (#81, CLAWP-118).
- Opt-in explicit scope: `sessions.Scope` plus `discovery.resolve_scope`, and a keyword-only `scope=` on `get_project_dir`, `get_repo_path`, `get_scoped_project_settings`, `get_tasks_dir`, `get_task` and `touch_task_updated`; `log add` migrated as the exemplar (#75, CLAWP-122).

### Changed

- MCP server tech-debt (#86, CLAWP-106): `tasks_state` gains `meta_reflect`/`process_lesson` (CLI parity); the `context` tool returns `project` as the plain id string with the metadata dict moved to `project_info` (BREAKING for consumers reading `context["project"]["id"]`); `ToolSpec.min_tier` is a `Literal` validated at construction; README no longer claims `--tools standard` widens the set; `build_agent_context` imports hoisted and docstring/module claims corrected.
- SKILL.md doc cleanup: document `-f/--format` as the only output control (no `--json`), the `uv tool install`/`pipx` shim-only install, `doctor -p/--project`, the post-batch `tasks list` check, and the CLAWP-109 Windows glob fix state (#82, CLAWP-110).

### Fixed

- Ignored-task-state probe (#90, CLAWP-135, follow-up to CLAWP-134): `project init` probes the allocator-resolved prefix (so a collision `CODE-B` is judged, not `CODE`), the probe skips numbers held in the git index (a deleted-but-unstaged tracked file no longer masks a blanket `.project/` ignore); a failed `git ls-files` or `git check-ignore` (git missing, timeout, unexpected exit), or an allocator/settings failure that forces a fallback probe prefix (malformed portfolio, failed sibling scan, malformed settings), now logs a `clawpm:` WARNING instead of a silent debug line, while "not a git repository", a missing `settings.toml` and a project with no portfolio entry stay quiet; and a fully used width (`GI-000`..`GI-999`) moves to width+1 instead of reusing a real file.
- Fail-open hardening: `detect_project_from_cwd`, `get_context_project` and `load_portfolio_config` now log a degraded-path warning instead of swallowing errors silently; `doctor --apply` keeps `applied[]` to genuinely applied items (errored/skipped/no-op attempts move to `apply_skipped[]` with an `outcome` field); the blocked-to-open cascade rewrites an existing stale `state:` frontmatter line (#83, CLAWP-094); the `updated:` stamp on state moves now preserves line endings byte-for-byte (CRLF no longer folded to LF on Linux, LF no longer expanded to CRLF on Windows).
- Research read path hardening: malformed research files are surfaced instead of silently dropped (`research list` JSON stays a flat array and reports them on stderr, `--with-diagnostics` opts into a `{research, malformed, malformed_count}` envelope; text section; MCP `research_list` always carries `malformed`/`malformed_count`; WARNING log); `Research.from_file` raises on unparseable/non-mapping frontmatter or non-list `tags` (`tags:` null = none); `add_research` uniques the frontmatter `id:` against every effective id (filename-stem ids included), under the research file lock, logging any file it cannot read (#84, CLAWP-095).

- A `git worktree move`d dispatched worktree now resolves to itself via its dispatch marker (only when the ledger's recorded path for that task/project is gone; the read path writes nothing, and teardown first persists the moved path into the ledger, aborting loudly if it cannot), and `tasks dispatch --worktree` fails closed with an actionable `branch_checked_out_elsewhere` error when `clawpm/<task>` is checked out at another path; the HEAD probe and `create_worktree` share one source repo (#80, CLAWP-117).
- `agent dispatch` now copies (never commits) its generated subtask into the worktree it creates and registers a session for that worktree, gated on the worktree carrying its own `.project/`; a failed copy or verification registers nothing and is reported loudly; the verdict transition is mirrored into the worktree copy (#79, CLAWP-115).
- When the session ledger (`sessions.jsonl`) is unreadable or holds no valid event, session-scoped resolution now falls back to the dispatch marker of the worktree the command runs in, instead of silently resolving to the main checkout (the CLAWP-098 corruption). Marker project must match; no marker keeps the registry lookup; every degraded path logs (#78, CLAWP-114).
- On Windows the CLI no longer glob-expands its own arguments (Click's `windows_expand_args`): `--scope "src/double-star"` is stored verbatim instead of failing with a usage error or being rewritten to a file path, and `~`, `$VAR` and `%VAR%` in free text survive too. Options that name a filesystem path (`--target-dir`, `--body-file`, `--scope-file`, `--in-repo`, ...) still expand `~` and environment variables, on every platform (#77, CLAWP-109).
- `tasks edit` (CLI and MCP `tasks_edit`) now merges predictions: passing one prediction flag overwrites only that field instead of nulling duration, confidence, pre-mortem, scope, `filled_by` and the rest (#76, CLAWP-108).
- Task-id allocation now consults a portfolio-level reservation ledger (`id_reservations.jsonl`), so two worktrees of one project no longer mint the same id (#74, CLAWP-092).

## [0.2.0] - 2026-10-05

Everything since the 0.1.0 baseline: the agentic layer (goal rubrics, Stop-hook judge, dispatch, leases), calibration analytics, concurrency safety, a CLI/service-layer refactor, an MCP server, and a long tail of task-ID and Windows-encoding fixes. PR numbers refer to `martinduncanson/clawpm`.

### Added

- `clawpm-cowork` skill bundled under `skills/` and mirrored on sync (38630fa).
- Goal integration: rubrics, Stop-hook evaluator, hook-based dispatch (#6, CLAWP-016..021).
- Leverage suite: Mission Control, reference-task surfacing, agent bridge, resume, `doctor --apply` (#7, CLAWP-022..026).
- CodeGraph integrations: scope auto-suggest, resume enrichment, agent worktree init, semantic reference scoring, doctor advisory (#9, CLAWP-027..031).
- `doctor --check-codex` codex-availability heuristic (#4, CLAWP-008).
- `doctor --check-encoding` AST scan for cp1252-risk patterns (#5, CLAWP-011).
- `doctor` semble content-shape advisory (#10, CLAWP-036).
- Decomposition and calibration arc, including the calibration loop (#11, CLAWP-037/038/040).
- Judge: adversarial confirm-close tier (#12, CLAWP-041); anchoring fix and tournament comparative selection (#14, CLAWP-043/044).
- Dispatch: crash-safe leases with TTL, heartbeat, expiry and fallback (#13, CLAWP-039); per-task `baseline_ref` and pre-dispatch drift reconciliation (#24, CLAWP-055); drift-not-checked marker (#27, CLAWP-063); thrashing and runaway detection (#29, CLAWP-062).
- Runtime next-action hints (#21, CLAWP-050); skill mirror auto-sync hook and capability map (#19, CLAWP-049).
- Tasks: `rejected` terminal state and won't-do ledger (#22, CLAWP-053); `out_of_scope`, `stop_conditions` and delegability contract fields (#23, CLAWP-054); tags and workstreams (#41, CLAWP-069); `updated` timestamp (#47, CLAWP-086); archive/prune for done tasks (#42, CLAWP-085); query/filtering and wiki-link backlinks (#49, CLAWP-082); cross-project `tasks list --all-projects` (#50, CLAWP-084).
- `emit-tree`: one-shot atomic task-tree emission with PRD storage (#25, CLAWP-056); hierarchical nesting via `parent_ref` (#30, CLAWP-064).
- Governing-principles constitution layer (#26, CLAWP-057).
- `clawpm-planner` skill: in-harness objective-to-task-tree judgment layer (#31, CLAWP-059).
- Uniform caller-contract exception mapping in the CLI (#35, CLAWP-067).
- Bulk state operations via varargs task IDs (#45, CLAWP-083).
- `introspect --json` machine-readable capability listing (#53, CLAWP-088).
- stdio MCP server interface (#54, CLAWP-068).
- Warning when `.project/` task state is git-ignored (#72, CLAWP-134).
- GitHub Actions test matrix and coverage reporting (#48, CLAWP-074).

### Changed

- Per-project file lock, then a reentrant `file_lock` with comprehensive mutator coverage (#33 CLAWP-051, #34 CLAWP-066); transaction integrity v4 with 2-phase atomicity and TOCTOU fixes (#44, CLAWP-071).
- `cli.py` decomposed into a `cli/` package plus service layer (#51, CLAWP-077).
- Web layer demoted to read-only (#37, CLAWP-078).
- Shared `parse_frontmatter` helper replaces 14 hand-rolled sites (#39, CLAWP-079).
- `list_tasks`/`get_next_task` skip state-excluded directories (#38, CLAWP-080).
- Test fixtures consolidated in `conftest` with thicker coverage (#40, CLAWP-081).
- Task-ID prefix allocation replaced greedy matching with bipartite matching (#64, CLAWP-124).
- Docs: SKILL.md reconciled with shipped behaviour (#36, CLAWP-073); dispatch rubric and worktree discipline (#17); README and AGENTS.md template brought current (#52, CLAWP-097); research entry template aligned (#43, CLAWP-087); planning merge-plan ADR and calibration metrics spec (#58, CLAWP-103/104).
- CLAWP-072 quick-fix batch: dead code, config, CLI ergonomics (#46).

### Fixed

- cp1252 stdout root-cause fix: stdio reconfigured to UTF-8 plus scan-clean guard (#15, CLAWP-045); git subprocess output decoded as UTF-8 (#20, CLAWP-046).
- Task IDs: auto-ID collision on hyphenated-prefix projects (#16, CLAWP-047); portfolio-unique prefixes (#18, CLAWP-048); reserve a task-less sibling's full prefix chain (#60, CLAWP-121); include `rejected/` in numbering scan (#62, CLAWP-127); portfolio-wide lock for first-mint allocation (#65, CLAWP-116); refuse explicit-ID creates that collide with another project's claim (#66, CLAWP-129) or a sibling's future mint (#68, CLAWP-130); never hand out a subtask-shaped prefix (#70, CLAWP-132); normalise legacy doubled-separator prefixes on disk (#71, CLAWP-113); resolve tasks nested 2+ levels deep (#69, CLAWP-131).
- Windows-safe `--scope-file` for glob-valued options (#28, CLAWP-060).
- Session-scoped resolution for worktree-dispatched mutators (#55, CLAWP-098).
- Frontmatter mutation sites guarded against non-dict data (#56, CLAWP-091).
- CLI ergonomics: combined duration units, prefix double-dash (#57, CLAWP-096).
- `emit-tree` idempotency recognises directory-shaped children (#67, CLAWP-128).
- `updated`/`created` stamped in UTC (#63, CLAWP-126); stale-blocked tests backdated by UTC date (#61, CLAWP-123).

## [0.1.0] - 2026-05-15

Initial baseline: the upstream `malphas-gh/clawpm` original (2026-02-20) plus the fork's first phases (ROADMAP Phases 0 to 1.8.1).

### Added

- Fork init: AGENTS.md template; `scope` field and `clawpm conflicts` (Phases 1, 1a).
- Reflection layer Phase 1: predictions, actuals, deltas, notes (Phase 1b).
- Applied-science fields: success_criteria, approach, unknowns, confidence, reference_tasks, pre_mortem, process_lesson, surprise_taxonomy (Phase 1.5).
- `clawpm doctor` checks, `clawpm reflect void`, `filled_by` field (Phase 1.6); `clawpm inbox` inter-agent messaging (Phase 1.7).
- `clawpm project announce` with auto-run on init; doctor commit-drift and missing-marker checks (#2, Phase 1.8).
- `issues add` accepts the `observation` type and repeatable `--tag`; `issues list` filters by `--type` and `--tag` (#3, Phase 1.8.1).

### Fixed

- TOML backslash and test-fixture fixes (Phase 1); Unicode/cp1252 and ID-collision fixes (Phase 1c); calibration loop fixes for duration units, subtask isolation, `files_changed` filter, unblock action and re-start warning (Phase 1d).
- `doctor`/`projects` tolerate non-UTF-8 markdown, dedup project IDs, drop non-cp1252 glyphs (#1).

[Unreleased]: https://github.com/martinduncanson/clawpm/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/martinduncanson/clawpm/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/martinduncanson/clawpm/releases/tag/v0.1.0
