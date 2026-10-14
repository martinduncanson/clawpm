# ClawPM reference: dispatch and scope

Moved verbatim from `SKILL.md`. Covers: Dispatch discipline detail: why a dispatched rubric must not cross an approval gate, the worktree-mutator history (CLAWP-098), and scope-aware dispatch with the glob-overlap check.

**A Stop-hook rubric is a hard mechanism that overrides soft policy.** A dispatched agent is hook-blocked from terminating until its rubric is satisfied, so the *fastest path to satisfaction is its incentive*. If that path runs through an action the agent is forbidden to take unilaterally but technically *can* (it holds the write capability), the hook actively incentivises the violation — e.g. an outcome rubric "the books balance" on a write-capable agent makes applying corrections *without* the required human approval the path of least resistance. The Stop hook has turned an approval gate into an obstacle to route around.

This is the dark-side counterpart to the rubric/Stop-hook power: a goal-scoped rubric is exactly as safe as the gap between "what satisfies it" and "what the agent is allowed to do." Keep that gap at zero.

**ID-based mutator commands are safe to run from inside a `tasks dispatch --worktree` checkout (CLAWP-098, fixed).** Prior to the fix, `tasks state/done/block <id>` run with cwd inside a dispatched worktree resolved the project via the global portfolio registry — which ignores cwd entirely — and silently mutated the MAIN checkout's task file instead of the worktree's own copy. `tasks dispatch --worktree` now mints a session pointer (`~/clawpm/sessions.jsonl`) mapping the worktree's filesystem path to the dispatch; any ID-based mutator run with cwd inside that path resolves against the worktree's own `.project/tasks/`, not the registry. This requires the worktree to actually carry its own `.project/` (true for any project that tracks it in git, per CLAWP-075) — a project that doesn't commit `.project/` still has nothing for a worktree-scoped mutator to act on, same as before. Normal single-checkout usage (cwd outside any dispatched worktree) is unaffected either way. The session stays live for as long as the worktree directory exists on disk — tearing down one task's dispatch settings does NOT retire its session (a bulk `tasks state A B done` run from inside a worktree needs both A's and B's lookups to keep resolving there for the whole invocation, even after A's own teardown fires mid-loop). **`clawpm agent dispatch` (CLAWP-024) takes its own path (CLAWP-115, #79).** It auto-creates its subtask uncommitted, then immediately worktree-checks-out committed HEAD, so the new task file does not land in that worktree on its own. It therefore copies (never commits) the task file into the worktree, verifies the copy, and only then registers a session for the worktree. It does this only when the worktree carries its own `.project/`; otherwise it registers no session. If the copy, its verification or the registration fails, it registers no session and logs the failure; the worktree's Stop hook then resolves the task from the main checkout.

## Scope-Aware Dispatch

ClawPM can act as a file-claim registry for parallel agent runs. When multiple
Claude Code subagents operate in parallel (e.g. in separate git worktrees), they
can collide on shared files. Use `scope` to prevent this.

### Workflow

**1. Declare scope when creating or updating a task:**
```bash
clawpm tasks add -t "Refactor auth layer" \
    --scope "src/auth/**" --scope "tests/auth/**"

clawpm tasks edit CLAWP-042 --scope "src/auth/**" --scope "tests/auth/**"
```

**2. When a task transitions to `progress` its scope becomes "claimed".** Any
`progress`-state task with a non-empty `scope` is considered in-flight.

**3. Pre-flight check — run `clawpm conflicts` before dispatching a new agent:**
```bash
clawpm conflicts --scope "src/auth/login.py"
# → {"conflicts": [], "queried_scope": ["src/auth/login.py"]}  ← safe to dispatch

clawpm conflicts --task CLAWP-099   # read scope from the queued task
# → {"conflicts": [{"task_id": "CLAWP-042", "overlapping_globs": [...], ...}]}
```

An empty `conflicts` array means no in-flight task claims overlapping files —
safe to dispatch. Exit code is always 0; read the JSON array.

### Glob-overlap heuristic

The check uses a prefix-based heuristic:
- Strip `**`/`*`/`?` to get the literal prefix of each pattern.
- Two patterns overlap if either prefix starts with the other.
- Examples: `src/auth/**` ∩ `src/auth/handlers/**` → overlap; `src/auth/**` ∩ `src/billing/**` → no overlap.
- May produce false positives (safe: errs toward flagging rather than missing collisions).
- Does not handle character classes or negation patterns.

### Output format

```json
{
  "conflicts": [
    {
      "project_id": "polymarket-arb",
      "task_id": "POLYM-007",
      "title": "Fix auth flow",
      "scope": ["src/auth/**"],
      "state": "progress",
      "started_at": "2026-05-05T12:00:00+00:00",
      "overlapping_globs": ["src/auth/**"]
    }
  ],
  "queried_scope": ["src/auth/**", "tests/auth/**"]
}
```
