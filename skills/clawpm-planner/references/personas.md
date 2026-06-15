# Personas — opt-in elicitation lenses (off by default)

Personas are **composable lenses on the prompt**, not separate agents or mandatory
roles. They add multi-perspective elicitation **when warranted** — and stay out of
the way otherwise.

## Default: OFF

On **s/m** objectives personas are not invoked. Forcing a three-persona round-trip
on a two-leaf objective is ceremony that bloats every run. Lean by default.

## When they switch on

- **l/xl** objectives (the scale where multi-perspective scoping earns its keep), or
- the operator asks explicitly: *"plan this as a PM"*, *"give me the architect's
  view"*, *"what would an analyst ask?"*

They are applied **within** the existing ideate/specify stages — **not** as extra
subagents and **not** as extra stages. (Persona-as-subagent was considered and
rejected: it multiplies cost, risks the depth>2 nesting smell, and the lens value
is captured far cheaper as a prompt section.)

## The three lenses

- **Analyst** — *"what's the real problem, and how would we know it's solved?"*
  Sharpens the objective and the PRD success definition. Surfaces the metric the
  plan must move. Good at catching a vague objective before it becomes vague leaves.

- **PM** — *"what's the smallest valuable slice, what's explicitly out of scope,
  what's the sequencing?"* Drives the vertical-slice cut and the `out_of_scope`
  fields. Good at resisting scope creep and ordering milestones.

- **Architect** — *"where does this break, what's the blast radius, what are the
  load-bearing constraints?"* Grounds risk via the graph (`impact`/`trace`),
  populates `stop_conditions` and `pre_mortem`, flags the constraints that become
  constitution checks. Good at the failure-mode pass.

## How to apply

Adopt one or more lenses as a framing instruction during ideate/specify:
*"Considering this as a PM: what is the minimum valuable slice and what is out of
scope?"* — then fold the answers into approaches, the PRD, and the leaf contracts.
A persona that doesn't earn its keep on a given objective simply isn't invoked.

The value (multi-perspective elicitation when warranted) without the ceremony (a
3-persona round-trip on a small objective).
