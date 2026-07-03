---
created: '2026-05-25'
id: CLAWP-032
predictions:
  approach: 1) classify every src/clawpm/ writer as safe/at-risk-low/at-risk-high;
    2) per writer choose tmp+os.replace OR cross-platform locking (portalocker MIT
    OR native msvcrt+fcntl wrapper); 3) MVP fix issues.jsonl with locking; 4) concurrent-writers
    test harness scripts/test_concurrent_writes.py spawning N subprocesses, asserting
    line count + each line parses; 5) flag SQLite migration if >3 racy hot paths
  complexity: m
  confidence: 3
  duration_min: 360
  files_scope:
  - src/clawpm/cli.py
  - src/clawpm/worklog.py
  - src/clawpm/reflect.py
  - src/clawpm/inbox.py
  - src/clawpm/dispatch.py
  - src/clawpm/discovery.py
  - scripts/test_concurrent_writes.py
  filled_by: agent
  hypothesis: If clawpm's hot-path file writes are made cross-process safe on Windows,
    the multi-session parallel-clawpm-call pattern (which the operator routinely uses)
    stops silently losing data. The TOML-backslash incident showed a similar class
    of invisible Windows-specific failure; fixing this systematically should eliminate
    the whole class.
  pitfalls: Picking too heavy a solution (lockfile when atomic-rename suffices). Adding
    portalocker dep for one writer instead of native msvcrt/fcntl. Tests passing on
    POSIX but failing on Windows — concurrent-writer harness MUST run on Windows.
  pre_mortem: 'Most likely failure: subtle interleaving of atomic-rename writers (tmp
    file collision when 2 processes pick the same tmp suffix). Mitigation: use random/PID-tagged
    tmp suffix. Secondary: tests pass on Linux CI but real Windows operator still
    hits the bug — manual validation on F:/Git/clawpm before claiming done.'
  predicted_iterations: 2
  reference_tasks:
  - feedback_toml_backslash_silent_swallow.md
  success_criteria:
  - criterion: Every hot-path writer in src/clawpm/ classified safe/at-risk-low/at-risk-high
    gradeable_signal: audit document with per-writer line citations + classification
  - criterion: Each at-risk writer has chosen remediation + one-line rationale (which
      of atomic-rename/locking/serialiser/SQLite and why)
    gradeable_signal: audit document remediation column populated
  - criterion: issues.jsonl fixed with cross-platform locking + tested via concurrent-writers
      harness
    gradeable_signal: scripts/test_concurrent_writes.py spawns N=10 subprocesses each
      adding one issue; final line count == 10 AND each line parses as JSON
  - criterion: Audit findings logged as clawpm observation
    gradeable_signal: clawpm issues list --tag concurrency-audit-2026-05-25 returns
      the entry
  - criterion: Codex review clean on the fix PR before merge
    gradeable_signal: fork PR has Codex final round response of no major issues
priority: 5
---
# Audit concurrent-write safety on Windows

Audit clawpm's hot-path file writes for cross-process safety on Windows where multiple Claude Code sessions can call clawpm CLI concurrently against the same portfolio or project. Spot-check found .agent/issues.jsonl uses plain append mode (cli.py:3806-3807) which is NOT atomic across Windows processes — torn writes corrupt JSONL. Full audit needed: predictions/portfolio JSON, worklog.py, reflect.py, inbox.py, dispatch.py, discovery.py. Per-task md files already use tmp+replace (safe). Solution per writer: atomic-rename for read-modify-write; portalocker or msvcrt.locking for append loggers. Discovered while red-teaming session-state.json concurrency (UPSKI follow-up).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

