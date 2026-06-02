# Spec: clawpm × workflow-runtime integration

> Status: **proposed, trigger-gated** — do NOT build this yet. Build it only when the
> activation trigger below is met. Authored 2026-06-01; premise corrected 2026-06-01.
> Companion runtime: `F:/git/workflows` (github.com/martinduncanson/workflows) — a
> model-agnostic, in-process JS workflow orchestrator (`runWorkflow(options)`), MIT, Node ≥22.

## Read this first: the premise that justifies the build

An earlier draft of this spec claimed *"clawpm's `dispatch.py` spawns one subagent session
per task and has no in-process fan-out."* **That is mechanically wrong, and the error matters
— it changes what this integration is for.** Get the mechanism right before you build:

- `clawpm tasks dispatch <id>` (`src/clawpm/cli.py` → `src/clawpm/dispatch.py`) **does not spawn
  any worker.** It writes a hook-wired `.claude/settings.local.json` into a target directory:
  a `Stop` hook (`clawpm hook eval-stop`), a `PostToolUse` progress logger, and a `SessionStart`
  rubric injector. That's it. It's *integration-by-construction* — it instruments a directory.
- The **worker** is launched separately, by whatever harness the operator/parent runs in that
  directory (Claude Code's Task tool, or the operator by hand). clawpm never owns the worker's
  inference call. It owns the worker's **rubric gate** (the Stop hook) and the worker's **judge**
  (`claude --print`, via `src/clawpm/judges/stop_condition.py`).
- Therefore: when the worker runs inside a harness that **already has a fan-out primitive**
  (e.g. Claude Code's built-in `Workflow` tool — the same parallel/pipeline/structured-output/
  journal-replay capability this runtime reimplements), the worker **already has fan-out**. A
  dispatched task can already "review 12 files in parallel and verify each finding" using the
  harness's native tool. This integration adds nothing to that path.

**So what gap does this integration actually close?** Harness-independent fan-out. The runtime
earns its place exactly where there is **no fan-out-capable harness in the loop**:

- clawpm dispatching against a **local model** (`--provider ollama`) — subscription-free,
  air-gapped, no Claude Code.
- clawpm running **headless** in cron / CI / a non-interactive runner with no agent harness.
- A **non-Claude agent harness** that lacks an equivalent `Workflow` primitive.

In those cases there is genuinely no in-process fan-out available, and a clawpm task that needs
parallel decompose→verify→converge has to either run serially in one call or be hand-split into
N tasks. The runtime is the missing fine-grained layer **for the harness-independent path only.**

## Activation trigger (build gate)

Build T1–T4 **only when at least one of these is concretely true** — not speculatively:

1. There is a real clawpm dispatch that must run against a **local / non-Claude model** and
   needs intra-task parallelism (fan-out review, research sweep, batch migration).
2. clawpm needs to run a **fan-out task headless** (cron/CI) with no agent harness present.
3. A consumer explicitly needs **model-agnostic, subscription-free** fan-out with clawpm's
   durable task/rubric layer on top.

If the dominant use is "clawpm task dispatched into Claude Code," **do not build this** — the
native `Workflow` tool already covers it, and the better investment is the authoring-pattern
playbook (`docs/playbooks/dispatch-fan-out.md`), not this subprocess bridge. Re-read the premise
section above and confirm you're not adding redundant plumbing.

## One-line goal (once the trigger is met)

Let a clawpm task **execute as a fan-out workflow** without requiring a fan-out-capable agent
harness: clawpm shells out to the workflow runtime, which decomposes the task across parallel
model calls, verifies results, and returns structured JSON — with **clawpm's existing rubric
judge as the workflow's final verify gate** and the **workflow's journal folded into clawpm's
work log**.

| Layer | Owner | Concern |
|---|---|---|
| **Task** | clawpm | what/when, durability, predictions, reflection, rubric |
| **Orchestration** | workflow-runtime | how to parallelize *one* task: fan-out, verify, converge |

This mirrors clawpm's existing precedent of shelling out to `claude --print` for its judge —
same subprocess pattern, new callee. The language boundary is JSON over stdin/stdout plus an
exit code. **No Python↔Node FFI.**

## Architecture (subprocess bridge)

```
clawpm (Python)                          workflow-runtime (Node/TS)
───────────────                          ──────────────────────────
clawpm dispatch --workflow <script>
   │  build context (task body,
   │  predictions, scope, args)
   │  spawn:  node run-workflow.ts
   │          --script <path>
   │          --journal <path>
   │          (context JSON on stdin)
   ▼                                      run-workflow.ts (NEW thin CLI in the runtime repo)
 subprocess  ───────────────────────►      reads argv + stdin JSON
                                            calls runWorkflow({ script, provider, journalPath,
                                                                args, budgetTotal, ... })
                                            prints RunResult as JSON to stdout
   ◄───────────────────────────────────   exit 0 + {meta, result, cacheHits, cacheMisses,
   │  parse RunResult JSON                                agentsSpawned, resumedIncomplete}
   │  fold journal -> work_log.jsonl
   │  run rubric judge on result/transcript
   ▼  (eval_stop) -> task done | re-loop
```

## Build tasks

### T1 — `run-workflow.ts` CLI in the workflow-runtime repo (`F:/git/workflows`)

A thin entry that adapts the existing `runWorkflow` / `runWorkflowIsolated` exports
(`src/index.ts`) to a process boundary. Contract:

- **Argv:** `--script <path>` (required), `--journal <path>` (required), `--budget <n>`,
  `--concurrency <n>`, `--max-fan-width <n>`, `--isolated` (use `runWorkflowIsolated`),
  `--provider <anthropic|ollama|mock>`.
- **Stdin (JSON):** `{ "args": <any>, "provider": { "name": "...", "model": "..." } }` — `args`
  becomes the workflow's `args` global (pass the task body, scope, predictions here).
- **Stdout (JSON):** the `RunResult` verbatim — `{ meta, result, journalPath, cacheHits,
  cacheMisses, agentsSpawned, resumedIncomplete }` (see `src/types.ts`).
- **Exit code:** `0` on success; non-zero on a thrown fatal (`BudgetExhaustedError`,
  `AgentCapError`, `FanWidthError`, structured-output failure — all exported from `src/index.ts`)
  with the message on stderr.
- Provider auth via env (`ANTHROPIC_API_KEY`, `OLLAMA_HOST`) — never on argv.

~40 lines on top of the existing exports. This is the only change needed in the runtime repo;
it is also independently useful (lets the runtime be driven from any language, not just clawpm).

### T2 — `clawpm dispatch --workflow <script>` (clawpm side)

Extend `dispatch.py` (or a sibling module) with a workflow execution path. **Note this is a
genuinely new execution mode** — unlike the existing `tasks dispatch` (which only writes hook
settings and returns), this path *runs* the workflow subprocess to completion and captures its
output. Keep the two modes clearly separated.

- Resolve the task (existing `tasks` machinery), build the **context JSON** from the task:
  `{ args: { objective: <title>, body: <task body>, scope: <scope globs>,
            success_criteria: <rubric source>, predictions: {...} } }`.
- Spawn `node run-workflow.ts --script <script> --journal <portfolio>/wf/<task-id>.jsonl`
  with the context on stdin. Reuse `dispatch._assert_safe_identifier` for the task-id that
  becomes the journal filename (path-traversal / injection guard, already in `dispatch.py`).
- Resolve the `node` binary explicitly; surface a clear error if Node ≥22 is absent (mirror the
  `FileNotFoundError` handling in `stop_condition._default_judge_invoker`).
- Capture stdout JSON. On non-zero exit, mark the task blocked with the stderr reason (T4 #4).

### T3 — Journal → work_log fold

The workflow journal (`wf/<task-id>.jsonl`, `started`/`result` events keyed by `v1:<sha256>`)
is an append-only event log like clawpm's `work_log.jsonl`. After the run, summarize it into
`clawpm log add --task <id> --action progress` entries (one per phase, or a rollup:
"workflow ran N agents, M cache hits, P confirmed findings"). Use `concurrency.locked_append`
for the work_log write (Windows append atomicity — see `src/clawpm/concurrency.py`; this is the
exact doctrine that was reverse-ported into the runtime's `Journal`).

### T4 — Rubric judge as the workflow's verify gate

Two options:

- **(a) Post-hoc (build this first):** after the workflow returns, run
  `evaluate_stop_condition(rubric, transcript=json.dumps(result))` from
  `src/clawpm/judges/stop_condition.py`. If `ok=false` and not `impossible`, re-dispatch the
  workflow (bounded retries). If `impossible`, block for triage. Reuses the judge **unchanged**
  and keeps the boundary clean. The journal's `v1:` content-addressed keying means a re-dispatch
  with the same script replays unchanged calls from cache — only the changed/failed branch re-runs.
- **(b) In-loop (defer to v2):** expose the judge to the workflow as a provider/tool so the
  workflow's own final verify stage calls clawpm's judge. Heavier; not worth it until (a) is
  proven in practice.

## Mapping table (concepts that already align)

| clawpm | workflow-runtime | Note |
|---|---|---|
| rubric / success_criteria | the verify stage's pass condition | T4(a) above |
| `judges/stop_condition` `{ok,reason,impossible}` | Layer B verify gate (fail-closed, impossible) | clawpm was the **donor** — already folded into the runtime's `docs/layer-b-authoring-guidance.md` |
| `work_log.jsonl` (append-only) | `journal.jsonl` (`v1:` keyed, append-only) | same event-sourcing shape |
| `dispatch._assert_safe_identifier` | `assertSafeIdentifier` (`src/safe-identifier.ts`) | clawpm was the donor — already ported |
| `concurrency.locked_append` | journal in-process append mutex | clawpm doctrine; runtime shipped an in-process variant |
| predictions / reflection | `args` in, `RunResult` out | feed predictions as args; reflect on `agentsSpawned`/`cacheHits` |

**These reverse-flow items (judge contract, safe-id, append atomicity) are already DONE in the
runtime.** They required no clawpm change — clawpm was the source. Do not re-do them.

## Acceptance criteria

1. `clawpm dispatch --workflow examples/small-model-review.workflow.js --task <id>` runs the
   runtime as a subprocess and returns confirmed findings as JSON folded into the task.
2. A failing rubric re-loops the workflow (bounded), a satisfied rubric closes the task, an
   `impossible` verdict blocks for triage — no infinite loop.
3. The workflow journal is summarized into `work_log.jsonl` (atomic append via `locked_append`).
4. A fatal cap (budget/agent/fan-width) surfaces as a **blocked task with the stderr reason**,
   not a silent partial.
5. Works on Windows (paths via forward slashes per clawpm's `repo_path` rule; Node ≥22).
6. Runs against a **local model** (`--provider ollama`) with **no Claude Code present** — this
   is the trigger case the whole integration exists for; if it only works with Claude in the
   loop, you've built the redundant path. Test this explicitly.

## Risks / open questions

- **Provider auth across the boundary** — the subprocess needs `ANTHROPIC_API_KEY` (or a local
  model). Inherit clawpm's env; document the local-model path (`--provider ollama`). Note: if
  `ANTHROPIC_API_KEY` is present, the Anthropic provider bills **pay-as-you-go API**, not a
  subscription — the local-model path is the subscription-free one. (Same cost caveat as the
  `claude -p` judge — see the cost note in the dispatch-fan-out playbook.)
- **Node dependency in a Python tool's critical path** — this integration puts Node ≥22 on the
  execution path of a dispatch mode. Gate it (T2 detects Node, errors clearly if absent) so the
  default Claude-Code-in-the-loop path never pays this cost. Run it through `install-gate` before
  adding the runtime as a vendored/linked dependency.
- **Determinism** — the runtime bans `Date.now`/`Math.random` in scripts; clawpm-authored
  workflow scripts must follow that (pass timestamps via the context `args`).
- **node:vm vs QuickJS** — default `node:vm` (async, fast) is fine for clawpm-authored
  (trusted) scripts; `--isolated` (QuickJS) only if scripts ever come from an untrusted source.
  Note the QuickJS spike **serializes** model calls (no in-sandbox concurrency yet) — see the
  runtime README "v2 — isolation spike". Don't pick `--isolated` for fan-out-heavy work until the
  promise-bridge lands.
- **Who authors the script?** — for v1, ship a small library of clawpm-blessed workflow scripts
  (review, research-sweep, migrate); dynamic model-authored scripts are a later step.

## References

- Runtime API: `F:/git/workflows/src/index.ts` (exports), `src/runtime.ts` (`runWorkflow`),
  `src/sandbox-quickjs.ts` (`runWorkflowIsolated`), `src/types.ts` (`RunOptions`/`RunResult`).
- Authoring guidance: `F:/git/workflows/docs/layer-b-authoring-guidance.md`.
- Gating policy: `F:/git/workflows/docs/layer-c-gating-policy.md`.
- Worked shapes: `examples/small-model-review.workflow.js`, `examples/plan-verify-converge.workflow.js`.
- clawpm side: `src/clawpm/dispatch.py`, `src/clawpm/judges/stop_condition.py`,
  `src/clawpm/concurrency.py`, `src/clawpm/agent.py` (judge invoker resolution).
- Companion playbook (build this regardless of the trigger): `docs/playbooks/dispatch-fan-out.md`.
