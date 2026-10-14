# ClawPM reference: command reference

Moved verbatim from `SKILL.md`. Covers: Full command reference (projects, tasks, scope conflicts, work log, research, issues, batch/agent dispatch, judge, leases, constitution, mission, admin, doctor), work-log actions, task states.

## Full Command Reference

### Projects
```bash
clawpm projects list [--all]            # List projects (--all includes untracked repos)
clawpm projects next                    # Next task across all projects
clawpm project context [project]        # Full project context
clawpm project init                     # Initialize project in current dir
```

### Tasks
```bash
clawpm tasks                            # List tasks (default: open+progress+blocked)
clawpm tasks list [-s open|done|blocked|progress|all] [--flat] [--all-projects]
                                        # --all-projects: portfolio-wide view across every ACTIVE project;
                                        # each row carries project_id, filters compose per-project (CLAWP-084)
clawpm tasks show <id>                  # Task details (includes scope)
clawpm tasks add -t "Title" [--priority 3] [--complexity m] [--parent <id>] [-b "body"] [--scope "glob/**"]
clawpm tasks edit <id> [--title "..."] [--priority N] [--complexity s|m|l|xl] [--body "..."] [--scope "glob/**"]
clawpm tasks state <id> open|progress|done|blocked [--note "..."] [--force]
clawpm tasks split <id>                 # Convert to parent directory for subtasks
clawpm tasks decompose <parent-id> --child "Title" --child '{"title":"...","success_criteria":["..."],"complexity":"m"}'
                                        # Split into child subtasks, each with its own rubric; parent
                                        # can't be DONE until all children are (CLAWP-037)
clawpm tasks emit-rubric <id> [--format markdown|outcome-payload]
                                        # Render success-criteria as a graded rubric (markdown for a
                                        # Stop-hook/human, outcome-payload for Anthropic's define_outcome)
clawpm tasks emit-tree [--dry-run] [--strict]
                                        # Persist a fully-contracted task-tree atomically from a JSON doc
                                        # on stdin. Validates all gates (reject-match, constitution,
                                        # ID-collision, baseline) before writing anything. Zero LLM calls.
                                        # This is the clawpm-planner emission target (CLAWP-056).
clawpm tasks dispatch <id> [--worktree] [--target-dir PATH] [--lease-ttl N]
                          [--fallback-policy requeue|route-secondary|escalate-to-human|fail]
                          [--confirm-close] [--refute-votes N] [--confirm-stale] [-f]
                                        # Write hook-wired .claude/settings.local.json so a hand-launched
                                        # subagent is Stop-hook-gated until the rubric is satisfied
                                        # (CLAWP-018). --worktree isolates parallel dispatches.
clawpm tasks teardown-dispatch [<id>] [--target-dir PATH] [-f]
                                        # Remove a dispatch settings.local.json (only clawpm-written ones
                                        # unless --force).
```

### Scope Conflicts
```bash
clawpm conflicts --scope "src/auth/**" --scope "tests/auth/**"
                                        # Check for in-flight tasks claiming overlapping files
clawpm conflicts --task CLAWP-042       # Same but reads scope from an existing task
clawpm conflicts --task 42 --project myproj
                                        # Explicit project for task lookup
```

### Work Log
```bash
clawpm log add --task <id> --action progress --summary "What I did"
clawpm log tail [--limit 10]            # Recent entries (auto-filtered to current project)
clawpm log tail --all                   # Recent entries across all projects
clawpm log tail --follow                # Live tail (like tail -f)
clawpm log last                         # Most recent entry (auto-filtered to current project)
clawpm log last --all                   # Most recent entry across all projects
clawpm log commit [-n 10]               # Log recent git commits to work log
clawpm log commit --dry-run             # Preview without logging
clawpm log commit --task <id>           # Associate commits with a task
```

Note: State changes (start/done/block) auto-log to work_log with git files_changed.

### Research
```bash
clawpm research list [--with-diagnostics]   # JSON = flat array; unparseable files reported on stderr, --with-diagnostics gives {research, malformed, malformed_count}
clawpm research add --type investigation --title "Question"
clawpm research link --id <research_id> --session-key <key>
```

### Issues
```bash
clawpm issues add --type bug --severity high --actual "What happened"
clawpm issues add --type observation --severity low --tag depth-warning --summary "depth>2 subagent nesting"
clawpm issues list [--open] [--type observation] [--tag depth-warning]
```
Types: `bug | ux | docs | feature | observation`. `observation` is for neutral signals worth logging (depth warnings, ergonomic gaps, calibration deltas) that aren't bugs. `--tag` is repeatable; `issues list --tag` matches any.

### Parallel batch dispatch
```bash
clawpm next                             # Single next task
clawpm next --batch                     # Next parallel batch (tasks sharing the lowest
                                        # open parallel_group; group N+1 waits for group N)
```
Tag siblings with `tasks add --parallel-group N` to make them dispatch together.

### Agentic dispatch (parent-spawned subagent, one command)
```bash
clawpm agent dispatch --prompt "<subagent brief>" \
    --rubric-criteria "Tests pass" \
    --rubric-criteria '{"criterion":"...","gradeable_signal":"...","comparator":"..."}' \
    [--parent <id>] [--title "..."] [--agent-profile <p>] [--confirm-close] [--refute-votes N] \
    [--no-codegraph] [--judge-cmd-override <cmd>]
```
Wraps the full cycle in one call (CLAWP-024): create subtask → worktree → dispatch settings →
subprocess `claude --print` → judge grade against the rubric → mark DONE (+ reflection event)
or BLOCKED (+ iteration event). Contrast with `tasks dispatch`, which only writes the hooks for
a subagent you launch yourself.

### Judge primitives
```bash
clawpm judge tournament --rubric-file <file> \
    --candidate <fileA> --candidate <fileB> [--candidate ...] \
    [--label <labelA> --label <labelB>] [--judge-cmd-override <cmd>]
```
Pairwise comparative selection across candidate deliverables (CLAWP-044). Each pair is judged in
both position orders to cancel position bias; ambiguous pairs keep the higher seed (pass the
strongest-prior candidate first). The winner is SELECTED, not certified — verify it through
`hook eval-stop`.

### Leases (crash-safe dispatch)
```bash
clawpm lease grant --task <id> --ttl <seconds> \
    [--fallback-policy requeue|route-secondary|escalate-to-human|fail] \
    [--holder <id>] [--target-dir <dir>]
clawpm lease heartbeat --task <id> [--holder <id>]   # holder is alive (wired to the dispatch hook)
clawpm lease list [-p <project>]                     # active leases + expiry + fallback
clawpm lease release --task <id>                     # clean completion, no fallback on later sweeps
clawpm lease sweep [--dry-run]                        # detect expired leases, apply fallback (no daemon)
```
A dispatched holder heartbeats while alive; if it goes silent past the TTL, a sweep (run by
`clawpm doctor` and on the next `tasks dispatch`) transitions the task per its fallback policy
(CLAWP-039). `tasks dispatch --lease-ttl` grants the lease inline.

### Constitution (named invariants that constrain emission)
```bash
clawpm constitution add -n <name> \
    -k require_success_criteria|max_complexity|require_scope|advisory \
    [-d "description"] [--param KEY=VALUE ...]
clawpm constitution list
clawpm constitution remove -n <name>
```
Code-checkable or advisory invariants enforced by `tasks emit-tree`'s constitution gate.

### Mission (macro binary-outcome layer above tasks)
```bash
clawpm mission add -t "Ship-able statement" -o "YES/NO check at deadline" [-d <days>] [-b "body"]
clawpm mission add-goal <mission-id> --task <id> [--actor agent|human]  # link a task as a mini-goal
clawpm mission list
clawpm mission tasks <mission-id> [--actor agent|human]
clawpm mission status <mission-id>                                       # progress + outcome state
clawpm mission state <mission-id> active|complete|failed|cancelled
```
Tasks are a mission's mini-goals; the mission is the binary outcome they serve.

### Admin
```bash
clawpm setup               # Create portfolio (first-time)
clawpm setup --check       # Verify installation
clawpm status              # Project overview
clawpm context             # Full agent context
clawpm doctor              # Health check (whole portfolio)
clawpm doctor -p myproj    # Health check scoped to one project (-p/--project)
clawpm doctor --strict     # Health check — exits non-zero if any warning (use in CI/pre-flight)
clawpm use [project]       # Set/show project context
clawpm use --clear         # Clear context
```

#### Doctor checks (Phase 1.6)
`clawpm doctor` now runs three additional diagnostic checks beyond basic file existence:

- **Stale tasks** — any `.progress.md` file not touched (mtime or work_log entry) in >7 days surfaces in `stale_tasks[]` with `days_stale` and `suggested_action`.
- **Filesystem-vs-state drift** — if a task file's frontmatter `state:` field disagrees with its location (`tasks/` = open, `tasks/done/` = done, etc.), it appears in `drift_tasks[]`.  Also flags half-renames (both `PROJ-001.md` and `PROJ-001.progress.md` exist simultaneously).
- **Prefix collisions** — two projects whose IDs share the same first-5 uppercase chars (the task-ID prefix) appear in `prefix_collisions[]`.  Colliding prefixes cause silent task-ID aliasing.

Doctor always exits 0; `--strict` exits 1 if any warning is present.

## Work Log Actions

- `start` - Started working (auto-logged on `clawpm start`)
- `progress` - Made progress (use `clawpm log add --action progress` for mid-task updates)
- `done` - Completed (auto-logged on `clawpm done`)
- `blocked` - Hit a blocker (auto-logged on `clawpm block`)
- `unblock` - Blocker resolved (auto-logged on `clawpm unblock`)
- `commit` - Git commit (logged via `clawpm log commit`)
- `pause` - Switching tasks
- `research` - Research note
- `note` - General observation

## Task States & File Locations

| State | File Pattern | Meaning |
|-------|--------------|---------|
| open | `tasks/CLAWP-042.md` | Ready to work |
| progress | `tasks/CLAWP-042.progress.md` | In progress |
| done | `tasks/done/CLAWP-042.md` | Completed |
| blocked | `tasks/blocked/CLAWP-042.md` | Waiting |

- **Portfolio root**: Default `~/clawpm`
- **Work log**: Append-only at `<portfolio>/work_log.jsonl`
