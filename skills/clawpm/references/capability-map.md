# ClawPM reference: capability map

Moved verbatim from `SKILL.md`. Covers: Which command to reach for in which situation (situation -> command table).

## Capability map — which command for which situation

clawpm is meant to be *agentic*: the acting agent should reach for the right primitive, not just `add`/`done`. Match the situation, not the noun.

| Situation | Reach for | Why |
|---|---|---|
| New multi-step work unit | `tasks add` with `--predict-*` + `--success-criteria` | The success_criteria become the gradeable contract for everything below. |
| One task is really several independent sub-pieces | `tasks decompose <id>` | Splits into child subtasks under the parent; each gets its own rubric. |
| Several queued tasks can run in parallel | set `--parallel-group N`, then `clawpm next --batch` | Returns the next dispatchable group; `tasks add` siblings sharing a group run together. |
| About to spawn a parallel agent on shared files | `clawpm conflicts --scope ...` (or `--task <id>`) | Pre-flight file-claim check; empty `conflicts` array = safe to dispatch. |
| Subagent doing >5min file work toward a verifiable goal | **see "Two dispatch modes" in `SKILL.md`** | The mode depends on *how* the subagent runs (separate process vs in-harness). |
| Verify a subagent's deliverable against its rubric | `subagent-judge` skill | Independent criterion-by-criterion grade; catches "said done, missed criterion N". |
| Render a task's rubric for a grader / outcome payload | `tasks emit-rubric <id>` | Markdown rubric, or `--rubric-format outcome-payload` for the Anthropic Outcomes shape. |
| Long/crash-prone dispatch that must survive a dead holder | `lease grant/heartbeat` (+ `tasks dispatch --lease-ttl`) | TTL + heartbeat; expiry → fallback policy (requeue/route/escalate). No daemon. |
| High-confidence "done" you don't fully trust | `eval-stop --confirm-close` (or `CLAWPM_CONFIRM_CLOSE`) | Spends an adversarial refutation pass only on the ok=true→close transition. |
| Calibrate future estimates from history | `reflect summarize` / `reflect suggest` | Actual/predicted ratios bucketed by complexity/confidence; deflates a gut estimate. |
| Macro binary outcome above the task layer | `mission` commands | The mission layer; tasks are its mini-goals. |
| After a merged PR | `commit-commands /clean_gone` + `tasks done` | Reaps gone branches; closing the task cascades unblocks. |
