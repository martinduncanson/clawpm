---
description: Plan an objective into a clawpm task tree (clawpm-planner skill)
argument-hint: <objective or brief>
---

Invoke the `clawpm-planner` skill on this objective: $ARGUMENTS

- Run the stages the objective warrants (scale dial: s/m/l/xl); default one tier lower when unsure.
- Emit through `clawpm tasks emit-tree`; never write task files by hand.
- If the objective has two or more open decisions, use chart mode (`/clawpm:chart`) instead.
- Show the operator the vetted tree before emitting at scale l or xl.
