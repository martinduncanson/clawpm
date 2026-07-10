---
baseline_ref: 0b307f2
complexity: s
created: '2026-07-03'
id: CLAWP-088
predictions:
  confidence: 3
  duration_min: 120
  filled_by: agent
  success_criteria:
  - clawpm introspect emits JSON of every command group/command with options, types,
    choices and help text, generated FROM the click registry (never hand-maintained)
  - A fresh agent can construct any valid invocation from introspect output alone
    (spot-verify 5 commands)
  - Output is stable-ordered for diffability; test asserts schema shape
priority: 6
scope:
- src/clawpm/cli.py
updated: '2026-07-10'
---


# clawpm introspect --json: machine-readable command/capability listing

Audit 2026-07-03 (CLI/UX): no machine-readable capability listing - a fresh agent must shell --help per group and parse human text, or read source. Biggest agent-ergonomics gap for an agent-first JSON-first tool.

SPEC: walk the click command tree (ctx.command.commands recursively) and serialize. SEQUENCE with CLAWP-068 (MCP server): the MCP tool schemas and introspect output should derive from the same walk - build introspect first as the cheap standalone win, then 068 consumes it. Also becomes the ground truth that doc-staleness checks (SKILL.md vs reality) can diff against.

ADDED SCOPE (operator, 2026-07-05): once introspect walks the click registry, wire a doc-staleness CI check (or clawpm doctor --strict rule) that diffs README's All-commands tables against introspect output -- fails on a shipped command/flag with no README mention. Keep narrative sections (architecture, walkthroughs) hand-authored; only gate the mechanical reference tables.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

