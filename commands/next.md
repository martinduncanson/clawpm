---
description: Show the next task, or the frontier of a decision map
argument-hint: [root task id]
---

Find what to work on next. Root given: $ARGUMENTS

- With a root id: run `clawpm next --frontier <root>` and list the ready, blocked and claimed leaves.
- With no argument: run `clawpm next`.
- Pick one leaf and claim it with `clawpm start <id>`. Take one decision leaf per session.
- Close with `clawpm done <id> --resolution "<outcome>"`. Refer to leaves by name, not bare id.
- Never answer a human-delegable leaf yourself; present options and wait.
