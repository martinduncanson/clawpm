# PRD / plan template — the `prd.body_markdown` artifact

The PRD is the **durable root anchor** a cheap executor reads in isolation. It is
emitted as the `prd` block of the emit-tree document (`emission-contract.md`),
stored as a research entry and linked to the tree root. Keep it **self-contained**
— an executor reading only this and one leaf should understand the why.

Fill this template into `prd.body_markdown`. Drop sections the scale doesn't need
(s = no PRD at all; m = the short core; l/xl = all of it, plus the ADR note).

```markdown
## Objective
<one or two sentences: the outcome, in user/reader terms — not the implementation>

## Why
<the problem / trigger. What's broken or missing today. Evidence if any.>

## Constraints
- <hard constraint — infra, budget, compatibility, deadline>
- <invariant this must respect — ties to the project constitution>

## Out of scope
- <explicitly NOT doing X — prevents the tree drifting into adjacent work>
- <direction candidates live in `clawpm research`, not here and not as leaves>

## Success definition
<the measurable bar the whole tree ladders up to. Every leaf's rubric must serve
this. Phrase it so a reviewer can check it: a metric, a behaviour, a cited artifact.>

## Chosen approach
<the one approach selected in ideate. One paragraph. Name the alternatives
considered and why this won — but keep the rejected ones in research, not here.>

## Open questions
- <unresolved decision the executor or operator must close — with the cheapest way
  to resolve it>

## Traceability
Ground consulted: <which graph (codegraph / graphify) was used, or "NONE — graph
unavailable">. <If none: name the remediation proposed and tag effort UNGROUNDED.>
Milestones / slices: <how the tree maps to this objective.>
```

## Notes

- **Success definition is the load-bearing section.** It's the contract the vet
  stage checks every leaf's rubric against (the ladder check). Spend effort here.
- **`type`** picks the research kind for the stored entry: `spike` (build/feature
  planning), `investigation` (research/analysis objective), `decision` (an ADR-style
  choice), `reference` (a standing doc). The two `examples/` use `spike` (software)
  and `investigation` (knowledge-work).
- **For xl, add a lightweight ADR note** under "Chosen approach" capturing the
  decision rationale — this design doc (`docs/design/CLAWP-059-planner-skill.md`) is
  itself the pattern.
- The "Traceability" section is where the **graph-absent remediation** is recorded
  durably — both demo PRDs use it to tag effort UNGROUNDED, satisfying the
  no-vibe-as-grounded requirement.
