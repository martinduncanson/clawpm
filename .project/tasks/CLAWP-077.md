---
baseline_ref: 0b307f2
complexity: l
created: '2026-07-03'
id: CLAWP-077
predictions:
  confidence: 3
  duration_min: 480
  filled_by: agent
  pre_mortem: 'Most likely failure: hidden coupling via module-level state or import
    cycles between the new cli modules; mitigate by moving groups one at a time with
    the suite green between moves'
  success_criteria:
  - cli.py split into per-group modules (cli/tasks.py, cli/log.py, cli/mission.py,
    ...) with cli.py as thin registration shell under 500 lines
  - A service layer function (e.g. tasks.transition) owns the state-change orchestration
    (change_state + rollup gating + worklog + reflection capture) and the CLI handler
    calls it; same for add/decompose/split orchestration
  - 'Zero behaviour change: full suite passes unmodified except import-path updates'
  - CLAWP-068 MCP server can call the service layer directly with no click dependency
    (write one demonstration test)
priority: 4
scope:
- src/clawpm/cli.py
- src/clawpm/cli/**
- src/clawpm/services/**
---
# cli.py decomposition + transition service layer (MCP enabler)

Audit 2026-07-03 (code-health): cli.py is 6,016 lines / ~87 commands / 15 groups. Read commands are thin, but MUTATION commands are fat controllers - tasks_state (cli.py:1648, ~180 lines) inlines surprise-tag validation, reject gating, parent rollup, git-diff detection, worklog + reflection capture; same shape in tasks_add, tasks_decompose (2041), tasks_split (2906), dispatch (4278). Policy/orchestration lives in the CLI layer while domain modules hold only primitives.

WHY NOW: CLAWP-068 (MCP server) needs exactly these orchestrated operations WITHOUT the click layer - its spec already mandates direct core calls, no subprocess. Sequencing this decomposition before/with 068 avoids implementing orchestration twice. Also unlocks: per-group modules end the 6K-line merge-conflict magnet, and the duplicate-inbox class of bug (two registrations in one file) becomes structurally impossible to miss.

SPEC: (1) create clawpm/cli/ package, move each click group; (2) lift shared mutation orchestration into a service layer consumed by both CLI and (later) MCP; (3) keep _mutation_errors mapping at the CLI boundary; (4) one group per commit, suite green between moves; (5) coordinate with CLAWP-071 (its TOCTOU/atomicity fixes land in the same code - sequence 071 first or fold).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

