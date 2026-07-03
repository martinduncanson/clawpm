---
created: '2026-06-01'
id: clawpm-research-workflows-runtime-and-clawpm-cross-pollination-plu
status: open
tags:
- workflows
- integration
- cross-project
type: investigation
---
# workflows runtime and clawpm cross-pollination plus integration

## Question

What does each project borrow from the other, and how should clawpm consume the workflow runtime?

## Summary

`F:/git/workflows` (github.com/martinduncanson/workflows) is a model-agnostic, in-process JS
**workflow runtime** — author orchestration as a JS script, run it in a sandboxed VM with
content-addressed replay, fan out via `parallel`/`pipeline`, verify, all behind a one-method
`ModelProvider` seam. clawpm is an **out-of-process** dispatch + rubric + reflection layer.
They are complementary, not redundant: clawpm owns what/when/durability; the runtime owns
how-to-parallelize-one-task. Bidirectional value found; one fix already shipped to the runtime.

## Findings

### clawpm -> workflow-runtime (borrows)

1. **Windows append atomicity (SHIPPED FIX).** `clawpm/concurrency.py` documents that Windows
   file appends are not atomic — concurrent appends interleave and silently corrupt JSONL. The
   runtime's `Journal` fired appends from many concurrent agents with no serialization (a real
   latent bug under fan-out). Fixed in the runtime via an in-process promise-chain append mutex
   (simpler than clawpm's cross-process file lock, because the runtime's writers are all
   in-process). Test: 50 concurrent appends -> 50 valid lines.
2. **Judge contract (FOLDED INTO Layer B).** `judges/stop_condition.py`'s `{ok, reason,
   impossible}` shape, the "impossible is evidence not proof — confirm independently" doctrine,
   and the refusal to coerce a malformed verdict to pass (fail closed, never fail open) are a
   sharper spec of the runtime's verify gate. Both the fail-closed rule and the
   unverified-vs-impossible distinction are now in `docs/layer-b-authoring-guidance.md`.
3. **Safe-identifier guard (PORTED).** `dispatch._assert_safe_identifier` (shell-injection /
   path-traversal guard) is ported to the runtime as `src/safe-identifier.ts` — the reference
   guard for v2 worktree-isolation / CLI id surfaces.

### workflow-runtime -> clawpm (the strategic direction)

clawpm dispatches ONE subagent per task; it has no in-process fan-out. A task needing "review
12 files in parallel and verify each" runs serially. The runtime is that missing layer. They
compose: clawpm = durable task layer; runtime = in-session fan-out. Integration is via
subprocess (clawpm shells `node run-workflow.ts`, captures RunResult JSON) — mirrors clawpm's
existing `claude --print` judge precedent. clawpm's rubric judge becomes the workflow's final
verify gate; the workflow journal folds into `work_log.jsonl`. Full buildable spec dropped at
`F:/Git/clawpm/WORKFLOW-RUNTIME-INTEGRATION.md`.

### On the hosted-feature description (planning / verification loop)

"Plans dynamically -> breaks into subtasks -> fans out -> checks results" is not a separate
planner module; the MODEL authors the JS orchestration (Layer B is the judgment, the runtime
is the execution). Reflected here via `examples/plan-verify-converge.workflow.js`: a planner
agent emits a structured subtask list, `parallel` fans out, lens-varied refute-verify checks
each finding, and a dry-round counter iterates until convergence.

## Conclusion

Complementary tools. Immediate wins (append fix, judge patterns, safe-id guard) are done in the
runtime. The high-leverage next step is `clawpm dispatch --workflow` per the integration spec —
it gives clawpm tasks fine-grained parallel execution without changing clawpm's durable-state or
rubric model. Build order: T1 (runtime `run-workflow.ts` CLI) -> T2 (clawpm dispatch path) ->
T3 (journal->worklog) -> T4 (rubric as verify gate, post-hoc variant first).
