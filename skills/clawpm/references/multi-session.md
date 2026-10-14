# ClawPM reference: multi session

Moved verbatim from `SKILL.md`. Covers: Supported multi-session model: branch-per-session, scope claims, per-project lock.

## Supported multi-session model (CLAWP-051)

Two agent sessions operating on the **same project** are safe by default.  Here is
the layered model, from outermost guard to innermost:

### Layer 1 — branch-per-session (git discipline)

Each session does its code work on its own branch (`session/<topic>-<YYYYMMDD>` or
`claude/<slug>`), not directly on `main`.  Branches are pushed early (after the first
commit); `main` advances only via PR merge.  This prevents concurrent git mutations
(rebase, reset) from orphaning another session's unpushed commits.  The concurrent-
session git discipline in CLAUDE.md is the authoritative reference.

### Layer 2 — clawpm `scope` claim (disjoint scopes avoid collision at source)

Before dispatching parallel agents use `clawpm conflicts --scope <glob-list>` (or
`--task <id>`) to check for file-claim overlaps.  An empty `conflicts` array means no
two tasks in the queue claim the same files; agents can run safely in parallel without
ever needing to co-ordinate their task-state writes.  When scopes overlap, serialise
the tasks or decompose them so they don't.

### Layer 3 — per-project file lock (serialisation backstop)

Even when two sessions end up touching the same project's task tree concurrently
(e.g. both call `add_task`, or both try to transition the same task), clawpm holds an
exclusive advisory lock for the duration of each mutation:

- **ID allocation (TOCTOU) — `add_task`:** acquires `<tasks_dir>/.clawpm-tasks.lock`
  before scanning existing IDs and holds it until the new task file is written to disk.
  Two concurrent sessions see different `next_num` values and write different task IDs.
  Explicit-ID creates are guarded against clobber: if the target file already exists,
  `FileExistsError` is raised rather than silently overwriting the prior task.
- **Subtask allocation (TOCTOU) — `add_subtask`:** acquires the same lock around the
  scan→mint→write critical section.  Two sessions decomposing the same parent concurrently
  see different `next_num` values and never mint the same subtask ID.
- **State-transition moves — `change_task_state`:** the entire read→validate→mutate→reload
  transaction is held inside a single lock acquisition.  This covers the REJECTED
  frontmatter rewrite, the DONE/rollup re-check, the `shutil.move`, and the return-value
  reload — all under the same critical section.  If the source file has already been moved
  by another session, the operation raises `FileNotFoundError` with a message naming the
  concurrent-session cause — no opaque crash, no silent lost-update.

**Granularity is per-project:** the lock file lives at `<tasks_dir>/.clawpm-tasks.lock`
and serialises mutations *within one project's task tree* only.  Different projects
run entirely independently.

**Lock implementation:** a non-blocking acquire polled in a backoff loop up to a
configurable timeout (~120s) — `fcntl.flock(LOCK_EX | LOCK_NB)` on POSIX,
`msvcrt.locking(LK_NBLCK)` on Windows — so a long-held lock (large rollup,
slow/AV-scanned filesystem) is waited out rather than failing at Windows'
hard 10s `LK_LOCK` cap.  Only genuine lock-contention errnos are retried; a
permanent fault on the sentinel fails fast.  Both are advisory and
cross-process.  No third-party dependency.

**Deadlock safety:** the lock is never held across subprocess calls or I/O-heavy
operations (e.g. `resolve_baseline_ref`'s git subprocess runs before acquiring).
`file_lock` is **reentrant per-thread** (CLAWP-066): a locked mutator may call
another locked helper on the same path (e.g. `add_subtask` → `split_task`) — the
nested acquire bumps a thread-local depth and the OS lock releases only when the
outermost block exits. Cross-thread/cross-process callers still serialise on the
OS lock. All task-tree mutators (`change_task_state`, `add_task`, `add_subtask`,
`split_task`, `edit_task`, mission/serve task writes) hold the lock.

**The `.clawpm-tasks.lock` file** is a runtime sentinel.  It is gitignored and never
committed; it does not interfere with task-ID scanning (it doesn't match task globs).

### When the backstop is enough vs. when you still need scopes

Locks co-ordinate **clawpm state** (task IDs, file moves); they do not co-ordinate
**git refs** or **application state**.  Two sessions that write to the same source
file will still produce a merge conflict regardless of clawpm locks.  The right tool:
disjoint scopes (Layer 2) for application-file safety; git discipline (Layer 1) for
ref safety; locks (Layer 3) for task-metadata safety.
