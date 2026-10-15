# Chart mode — emit a decision map, then stop

Chart mode is the planner's answer to "we cannot slice this yet": the objective
has open decisions that decide what the build leaves even are. Instead of
guessing leaves, emit the **decisions** as leaves, wire the build slices behind
them, and stop. Resolving decisions later sharpens the map.

It is a **rule, not an option** (SKILL.md hard rule 7). If the trigger fires,
chart. Do not decompose build leaves around an unresolved decision.

## Trigger

Run chart mode when either holds:

1. After ideate, **two or more open decisions block decompose** — each is a
   choice whose answer changes the shape, scope or order of the build slices.
2. The operator says "chart this" (or `/clawpm:chart`).

One blocking decision is not a map: record it as a single `kind: decision` leaf
in the normal tree. Scale: s never charts; chart mode sits between m and l (see
`scale-dial.md`).

## Procedure

1. **Name the destination.** Specify the destination only — one sentence stating
   the target state ("Users can import accounts from a CSV, bad rows reported,
   good rows saved, behind a flag"). Use the interview discipline in
   `stage-playbook.md` (specify): one question at a time, look facts up, ask only
   decisions, never take "sounds good" or silence as confirmation.
2. **List the open decisions.** One `kind: decision` leaf each, titled as the
   question ("Decide: validate rows before or during the save"). Rubric: a
   resolution is recorded (`gradeable_signal`: resolution field non-empty).
3. **Route each decision by who can answer.** `delegability: human` for a
   question only the operator can settle (the agent never answers its own
   grilling question). `delegability: agent` for a research question a worker can
   settle from the ground; these can be dispatched at once.
4. **Wire the build slices behind them.** Each build leaf that waits on a
   decision lists it in `depends_refs` (ref of a leaf in this document, or
   `id:<task-id>` for an existing task). A build leaf may also depend on another
   build leaf. Build leaves stay few and coarse: only slices you can already
   state sharply.
5. **Park the fog.** Anything you cannot yet phrase as a precise question goes in
   `root.not_yet_specified`. Test: "can I state the question precisely now?" If
   not, it is fog, not a leaf.
6. **Emit once, then stop.** Dry-run, emit, surface `rejected` and
   `constitution_violations`, and stop. No further decomposition and no dispatch
   of build leaves (hard rule 5 applies). Research (`agent`) decision leaves may
   be dispatched immediately if the operator asks.

## Output shape

One `emit-tree` document, `schema_version: 1`:

| Field | Chart-mode use |
|---|---|
| `root.title` | "Decision map: <objective>" |
| `root.destination` | the target state from step 1 (new root only) |
| `root.not_yet_specified` | the fog list from step 5 (new root only) |
| leaf `kind` | `decision` for questions; omit for build slices |
| leaf `depends_refs` | the decisions (and slices) this leaf waits on |
| leaf `delegability` | `human` or `agent` per step 3 |

Copy the shape from `examples/decision-map.emit.json`: D1 and D2 are decisions,
B1 waits on D1, B2 waits on D2 and B1, and the root carries `destination` plus
two fog entries. Contract details: `emission-contract.md` (Decision-map keys).

## No-fog exit

If charting surfaces fewer than two open decisions and nothing worth parking as
fog, say so plainly ("no fog: nothing here blocks slicing") and offer plain
decompose. Do not emit a decision map for the sake of the mode.

## Working the map afterwards (not part of the chart run)

- Frontier: `clawpm next --frontier <root>` (or `tasks list --frontier <root>`)
  shows open, unblocked, unclaimed leaves.
- One decision leaf per session. Claim it with `clawpm start`; close it with
  `clawpm done <id> --resolution "<text>"`.
- When a fog entry becomes sharp, file it as a leaf under the root with
  `tasks add --parent <root> --graduates "<fog text>"`.
- Refer to decisions by name in narration, never by bare id.
