---
description: Chart a decision map for an objective (clawpm-planner chart mode)
argument-hint: <objective or brief>
---

Invoke the `clawpm-planner` skill in chart mode on this objective: $ARGUMENTS

- Follow `references/chart-mode.md`: name the destination, list the open decisions, park the fog.
- Emit a decision map (decision leaves, build leaves that wait on them, root fog) via `clawpm tasks emit-tree`.
- Mark each leaf `human` or `agent` delegable. Never answer a human leaf yourself.
- If nothing blocks slicing, say "no fog" and fall back to `/clawpm:plan`.
