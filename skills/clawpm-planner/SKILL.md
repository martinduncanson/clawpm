---
name: clawpm-planner
description: >
  Turn a free-text OBJECTIVE (software OR knowledge-work) into a vetted,
  scale-appropriate clawpm task-tree and EMIT it via `clawpm tasks emit-tree`.
  Runs the planning judgment in-harness on a capable model: constitution →
  recon (graph-grounded) → divergent ideation → PRD/spec → vertical-slice
  decomposition → vetting against the won't-do ledger + constitution →
  transactional emission with per-leaf rubric/scope/stop/delegability/
  predictions/baseline. Direction candidates are filed as research entries,
  not leaves. Delegable leaves hand off to a cheaper executor via clawpm
  dispatch. Triggers: "plan this objective", "break this goal into tasks",
  "decompose <objective>", "turn this brief/PRD into a clawpm tree", "what's
  the plan for <goal>". Also has an explicit `audit` mode ("audit this project")
  that fans out read-only dimension agents and plans their findings into a tree;
  otherwise NOT a code auditor. NOT for a single already-well-formed task (use
  `clawpm tasks add`).
license: MIT
user-invocable: true
metadata: { "openclaw": { "homepage": "https://github.com/martinduncanson/clawpm", "upstream": "https://github.com/malphas-gh/clawpm", "requires": { "bins": ["clawpm"] }, "emoji": "🗺️" } }
---

# clawpm-planner

Plan a free-text objective into a clawpm task-tree, vet it for slop, and **emit
it through the deterministic core** (`clawpm tasks emit-tree`). The skill is the
**only** place model judgment lives; core makes **zero LLM calls** — it receives
a fully-contracted tree and persists it atomically.

**The seam: the skill decides, core persists.** Every judgment (what, how to
slice, who executes, effort/risk) is made here. Core only writes facts.

## When to use

Use when an **objective needs planning** — multiple steps, non-obvious slicing, or
cross-session. Works for **software** *and* **knowledge-work** (brief / research /
notes). Not a general code auditor — it plans toward a goal. The one exception is
the explicit **audit mode** below, which turns dimension-agent findings into a tree.

**Skip** (use `clawpm tasks add`): a single well-formed task; pure Q&A.

## Stage flow — stages, not gates

All stages are **optional and composable**; select what the objective warrants and
re-enter freely. The **scale dial** (s/m/l/xl) governs how many run and how deep
decompose goes — `references/scale-dial.md`.

| # | Stage | Does | Key tool |
|---|-------|------|----------|
| 1 | **constitution** | Load project invariants that constrain every leaf | `clawpm constitution list` |
| 2 | **recon** | Orient on the ground; map structure/blast-radius | **graph** (codegraph/graphify) + parallel **Explore** |
| 3 | **ideate** | Diverge: approaches **and** direction candidates | model judgment; web; `clawpm research` |
| 4 | **specify** | Draft the PRD/spec (what + why) | `references/plan-template.md` |
| 5 | **decompose** | Vertical-slice leaves, each draft-contracted | model + graph `impact`/`trace` |
| 6 | **vet** | Re-read ground; reject dup/by-design; diff vs won't-do ledger | graph + `clawpm tasks list --state rejected` |
| 7 | **emit** | One transactional persist of the contracted tree + PRD | **`clawpm tasks emit-tree`** (stdin JSON) |
| 8 | **handoff** | Dispatch delegable leaves; bounce human leaves | `clawpm tasks dispatch` |

Scale shortcut: **s** = recon(light) → decompose → emit (1–2 flat leaves, no PRD).
**xl** = full pipeline + opt-in personas + direction-candidate review. Default one
tier **lower** when unsure. **Chart mode** (hard rule 7) sits between m and l.

`references/stage-playbook.md`: recon fan-out (`feature-dev:code-explorer`),
specify interview discipline, l/xl approach lenses (minimal / clean / pragmatic).

## Hard rules

1. **Leaves are vertical slices.** Each leaf is independently verifiable
   end-to-end, never a horizontal layer ("user can save a draft", not "write the
   DB schema"). Each carries a **rubric** (`success_criteria`, WHEN/THEN phrased)
   plus `scope` / `out_of_scope` / `stop_conditions` (seed the default stop-list) /
   `delegability`. See `references/decompose-vet.md`.

2. **Direction candidates are research, not leaves.** Alternative objectives /
   adjacent opportunities surfaced in ideate go to `clawpm research add` —
   **never** interleaved as leaves. Only the *chosen approach* crosses into
   decompose.

3. **Graph = facts, fan-out = judgment.** In recon/decompose, consult a graph
   (codegraph default; graphify for mixed corpora) for blast-radius and structural
   claims. If **neither is available**, surface the gap, propose remediation
   (`codegraph init -i`), and **tag every estimate UNGROUNDED**. See
   `references/graph-consultation.md`.

4. **Vet is the no-slop gate.** Before emit: re-read the cited ground, reject
   by-design behaviour and duplicates, and **diff against the won't-do ledger**
   (`clawpm tasks list --state rejected`), fuzzy matches included; say *why* a
   near-duplicate was rejected.

5. **Emit then PAUSE.** The emit stage runs **one** `clawpm tasks emit-tree`
   call and stops. Do **not** auto-dispatch. Hand off only when the operator says
   "and run them" (handoff stage).

6. **Idempotent re-emit via `attach_to`.** Each leaf carries a stable `leaf_key`.
   To top up a tree, re-emit with `root.attach_to: <root-id>` — core skips leaves
   whose `leaf_key` exists under that parent. A *new-root* emit mints a new parent
   and does **not** dedup, so never re-run one to "top up".

7. **Chart when decisions block (rule, not option).** Trigger: after ideate, **≥2
   open decisions block decompose**, or the operator says "chart this". Then:
   specify the *destination only* (interview discipline: `stage-playbook.md`),
   and emit **one decision map** — new root with `destination` + `not_yet_specified`
   (the fog), `kind: decision` leaves (`human` = operator answers, `agent` =
   research), build leaves with `depends_refs` on the decisions they wait on —
   then **STOP**: no build decomposition, no dispatch. Fog test: name a question
   only if you can state it precisely now. **No-fog exit:** if charting finds <2
   open decisions and nothing worth parking as fog, say so and offer plain decompose. s never charts. Procedure and
   shape: `references/chart-mode.md`; copy `examples/decision-map.emit.json`.

## Audit mode

Trigger: "audit this project", "project health audit", "run the audit". Audit is a
mode of this skill, not a separate skill; design and rationale:
`docs/design/2026-10-19-project-health-audit-pipeline.md`. The planner still is
not a general code auditor: it fans out read-only dimension agents, then plans
their findings into a task tree. Stage subset: **constitution → fan-out →
collate (CLI) → vet + contract completion → emit → PAUSE** (hard rule 5). No
ideate or specify. There is no decompose stage: the leaves come from findings,
but collate cannot know a leaf's contract, so vet completes it (step 4).

1. **constitution** — as stage 1.
2. **fan-out** — dispatch ONE agent per dimension, all in a **single message**
   so they run in parallel. Dimensions: `correctness`, `security`, `consistency`,
   `tests`, `dead_code`, `tooling`, `agentic_setup`. The operator may name a
   subset; dispatch only those. A full audit is 7 dimensions but many more agent
   runs once each is scoped to the limits below (29 on this repo): compute the
   real count from the scoping step, say it before dispatching, and offer the
   subset.
   - **Agent type: `auditor`** (or `general-purpose` where `auditor` is not in the
     roster). Never `Explore`: observed 2026-10-20, Explore agents stop at a
     15-turn limit and deliver no report at all, silently.
   - **The orchestrator scopes each dimension** to an explicit file list, never
     "the whole project". Build it from `git ls-files`, filtered per dimension,
     at most about 40 files AND 8,000 non-empty lines per agent. Split a bigger
     dimension repeatedly (by directory or package, then by file chunks in path
     order) until EVERY agent's scope is within BOTH limits, and merge the JSON
     lists of the agents on one dimension into one `<dimension>.json`. Count
     with `git ls-files -- <pathspecs>` and, per file, non-empty lines
     (`Get-Content $f | Where-Object { $_.Trim() }`). Measured on this repo
     (2026-10-20; source = `src/**`, shared by four dimensions): `tests` 127
     files / 47,421 lines needs 7 agents; source 77 files / 32,398 lines needs 5
     agents per dimension; `tooling` (6 files, 194 lines) and `agentic_setup`
     (35 files, 2,850 lines) need 1 each. Total for the 7 dimensions: 29 agents,
     not 7. Worked example of the file sets:
     `tests` = the test tree (`tests/**`); `tooling` = CI workflows, lint/format
     config, `pyproject.toml`/`package.json`, scripts; `agentic_setup` =
     `CLAUDE.md`/`AGENTS.md`, `.claude/**`, skills, hooks, `.project/SPEC.md`;
     `dead_code` = source files plus the list of their imports/callers from the
     graph where available; `correctness`, `security` and `consistency` = the
     source tree, split by package.
   - **The orchestrator pastes the won't-do ledger and constitution text into
     each brief** (`clawpm constitution list`, `clawpm tasks list --state
     rejected`, run once from the main checkout, not a worktree). Agents must not
     run those commands themselves.
   - The orchestrator records the dimensions it dispatched and writes each
     agent's JSON list (without the `COVERAGE:` line) to
     `<findings-dir>/<dimension>.json` (use a fresh scratch directory).
   - **Accept a reply as covering its scope only if all three hold**: the JSON
     list parses, a `COVERAGE:` line is present, and every file the orchestrator
     assigned to that agent (the scoped file list it built above) is accounted
     for, either read or named in `unread:`. A reply with valid JSON but no
     `COVERAGE:` line is incomplete, not a success; `[]` plus full coverage is
     the only way to say "nothing found". Files named in `unread:` are treated
     exactly like a failed agent's scope, below. The coverage line is the agent's
     own claim and core cannot verify it, so the vet stage spot-checks.
   - **An agent that returns nothing, malformed JSON, no `COVERAGE:` line, or
     unread files**: a retry must preserve its ORIGINAL scope. Re-dispatch ONCE
     as several narrower agents that together cover ALL of the failed agent's
     files (for unread files, exactly the unread files). A valid reply on a narrower
     list does not make the dimension covered by itself: a dimension counts as
     covered only when every file in its original scope was covered by a
     successful agent. Files still uncovered after the retry make the audit
     INCOMPLETE: report "dimension X: files A..B not covered" (name the files or
     path range) in the report and in the PAUSE summary, or stop and ask the
     operator. Never drop files silently and never present a partial audit as
     complete. If a dimension has NO successful agent at all, drop it from
     `--dimensions-run` too; if only part of it is covered, keep it in
     `--dimensions-run` (it did produce a file) and rely on the stated gap.
3. **collate** — deterministic, zero LLM calls:

   ```bash
   clawpm audit collate --findings-dir <dir> --dimensions-run a,b,c \
     --root-title "<title>" [--max-leaves N] > audit.json
   ```

   `--dimensions-run` is the list of dimensions that actually produced a file in
   step 2, so a missing file for a dispatched dimension fails loudly (by design)
   and is distinguishable from a valid empty `[]`. A dimension with no successful
   agent after the retry has no file and is not in the list. `--max-leaves` defaults to 40.
   `--findings-dir` is the only input style. Output is
   `{nothing_to_emit, document, report}`. Show the operator `report` (dimensions
   run, findings per dimension, `dropped_findings`). Save `.document` to a file;
   that file is what goes to `clawpm tasks emit-tree`.
   - `nothing_to_emit: true` is a successful no-op: report "audit found nothing"
     (naming the dimensions run) and do **not** call emit-tree.
   - Any non-zero exit lists every problem. Fix the findings files or re-dispatch
     the failed dimension. Never hand-edit around it.
4. **vet + contract completion** — findings are untrusted model output over repo
   content. For each leaf, re-read the cited `file:line`; drop by-design
   behaviour and anything matching the won't-do ledger (hard rule 4), and say
   why. Delete dropped leaves from the saved `.document` file.

   Then, **before** `emit-tree --dry-run`, complete the contract of every
   surviving leaf (hard rule 1). `collate` is deterministic and emits only
   `title`, `leaf_key`, `success_criteria`, `scope` and `delegability: either`,
   so you add the rest by editing the saved `.document` JSON, per the per-leaf
   contract in `references/decompose-vet.md`:
   - `stop_conditions` — at least one "if X is false, STOP and report" entry. Seed
     the **default stop-list** from that file: a leaf whose scope or finding
     touches auth/session, schema migration, bulk deletion, secrets/credentials,
     payment or prod config gets a seeded entry for **each** matching category
     (e.g. "STOP and surface to the operator before any auth/session change").
     Judge by the finding's `scope`, title and evidence, not the dimension name:
     a `tests` or `consistency` finding in `src/auth/**` still matches.
   - `out_of_scope` — what the fix must not touch (usually everything outside the
     cited file and its direct callers).
   - `delegability` — replace the default `either` with `agent`, `human` or
     `either` on your judgment; sensitive categories above lean `human`.
   - `predictions` — `duration_min`, `complexity`, `confidence`, `approach`,
     `pre_mortem`, `filled_by: "agent"`. Ground effort with a graph where
     available, else say "UNGROUNDED" in `approach` (hard rule 3).

   Do not edit `ref`, `leaf_key` or `success_criteria`: the keys carry
   idempotency (below) and the rubric already matches the finding.
5. **emit** — `cat <document-file> | clawpm tasks emit-tree --dry-run`, then the
   real call, then **PAUSE** for operator review. Do not dispatch. The PAUSE
   summary repeats every "dimension X: files A..B not covered" gap from step 2.

**Re-runs** are idempotent through each leaf's `leaf_key` and `root.attach_to`
(hard rule 6): to top up, replace the document's `root` with
`{"attach_to": "<root-id>"}` before emitting. A finding whose line moved gets a
new key and may re-emit once; vet and the won't-do gate are the backstop.

Dimension agent brief (copy; fill `<dimension>`, `<files>`, `<cap>` (default 15),
`<ledger>` and `<turns>` (default 25)):

```text
You are auditing the dimension "<dimension>". Audit ONLY these files:
<files>
READ-ONLY: do not edit, create or delete files, and do not run mutating
commands. Do not run clawpm commands. By-design behaviour, do NOT report:
<ledger>
BUDGET: about <turns> tool turns. Read the files, then answer; if the budget runs
low, return the findings you have so far rather than nothing, and list the
assigned files you did not read on the COVERAGE line.
Your FINAL message is the JSON list of findings (no code fence), then ONE last
line, exactly: COVERAGE: <n_read>/<n_assigned> files; unread: <comma-separated
paths, or none>
Every assigned file must be counted as read or listed unread. `[]` is a valid
answer only with full coverage; never pad. Each finding has exactly these keys:
  {"dimension": "<dimension>", "file": "<repo-relative path>", "line": <positive
   integer or null>, "title": "<short defect statement>",
   "severity": "critical|high|medium|low|info", "evidence": "<file:line and the
   offending text>"}
Report at most <cap> findings, highest severity first. Cite only code you read.
Do NOT report style preferences with no defect.
```

## The emit call

The emit stage builds one JSON document and pipes it to the CLI on stdin:

```bash
cat tree.json | clawpm tasks emit-tree --dry-run    # validate all gates, no writes
cat tree.json | clawpm tasks emit-tree              # promote atomically
```

Core validates (schema, reject-match, constitution, ID-collision, baseline), then
promotes the whole subtree all-or-nothing and returns `{ emitted, rejected,
constitution_violations, research_id, root_id, baseline_ref }`. Surface `rejected`
and `constitution_violations`; for violations, loop back to decompose/vet.

Exact schema: `references/emission-contract.md`. CLI-validated examples are in
`examples/` — read them before constructing a tree.

## Model tiering

A **capable** model plans. Emitted `agent`/`either` leaves dispatch to a cheaper
executor via `clawpm tasks dispatch`, gated by the Stop-hook judge + delegability
gate. `human` leaves bounce to the operator and never auto-dispatch.

## Personas (opt-in, off by default)

On **l/xl** or on request, adopt analyst / PM / architect prompt lenses in
ideate/specify (not subagents or stages): `references/personas.md`. Off at s/m.
