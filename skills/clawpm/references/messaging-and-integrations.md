# ClawPM reference: messaging and integrations

Moved verbatim from `SKILL.md`. Covers: Inter-agent inbox messaging, workflow integrations and review doctrine.

## Inter-agent messaging — clawpm inbox

Filesystem-first, append-only, no-daemon messaging between agents. Each agent has its own
JSONL file at `~/clawpm/inbox/<agent-id>.jsonl`. Events are never rewritten or deleted —
acks are events too. Survives compaction and reboots; no polling daemon required.

Storage: `~/clawpm/inbox/<agent-id>.jsonl` (created on first send).

| Command | Description |
|---------|-------------|
| `clawpm inbox send --to <id> --message "..."` | Send a message to an agent's inbox |
| `clawpm inbox read --agent <id> [--unacked\|--all]` | Read messages (default: unacked only) |
| `clawpm inbox ack <msg-id> [<msg-id>...] [--agent <id>]` | Acknowledge messages |
| `clawpm inbox thread <msg-id>` | Show full thread (walks in_reply_to chain across all inboxes) |
| `clawpm inbox send --to <id> --message "..." --task-request --title "..." [--project <id>] [--success-criteria ...] [--predict-duration 2h] ...` | Hand off work as a structured `task_request` payload (sender never writes `.project/`) |
| `clawpm inbox materialize [--agent <id>] [--project <id>] [--dry-run]` | Recipient-side: turn pending `task_request` messages into tasks via the normal add path, reply with the new task id (`in_reply_to`), ack. `--dry-run` writes nothing. Run at natural checkpoints (session start, between dispatches). Invalid requests get a `task_request_rejected` reply and are acked; unresolvable projects stay pending and are listed under `failed`. |

Optional send flags: `--from <id>` (default `main`), `--in-reply-to <msg-id>`, `--project <id>`, `--task <id>`.
Read filters: `--since <YYYY-MM-DD or ISO>`, `--from <sender>`.

**Worked example — parent dispatches subagent, receives results:**

```bash
# 1. Parent sends pre-context to subagent at dispatch
clawpm inbox send --to researcher --message "Find pricing data for POLYM-007" \
    --project polymarket-arb --task POLYM-007
# → {"msg_id": "INBOX-20260508-a3f9", "to": "researcher", "ts": "..."}

# 2. Subagent reads inbox, acks, does work, sends results back
clawpm inbox read --agent researcher          # returns the message
clawpm inbox ack INBOX-20260508-a3f9 --agent researcher
clawpm inbox send --to main --message "Pricing data: BTC-USD spread 0.3%" \
    --in-reply-to INBOX-20260508-a3f9 --project polymarket-arb

# 3. Parent reads results (unacked by default)
clawpm inbox read --agent main
```

## Workflow Integrations

clawpm is the task layer. Other skills/plugins handle specialised lifecycle moments. Suggest these to the operator at the matching points — don't force them, but don't stay silent either.

### Skill suggestions by lifecycle moment

| Moment | Suggest | Why |
|---|---|---|
| Task creation, before any code | **`feature-dev`** | 7-phase guided workflow (explore → ask → architect → plan → implement → review → reflect). Particularly valuable when complexity is `l`/`xl` or `predict-pitfalls` is non-empty. Seed clawpm subtasks per phase. |
| Before commit | **`commit-commands /commit`** | Auto-drafts commit message from staged changes; we can add `Closes <task-id>` to the message and capture the SHA in `clawpm log commit` after. |
| Between commit and push (non-trivial diff) | **PRE-REVIEW subagent** | Reviewer subagent on the diff with no other context — catches what the implementer missed in self-review. See `code-quorum` §3 for the canonical rule (skip for pure docs/config or ≤50 LOC AND mechanical; never skip for auth/serialization/data-storage/silently-failing invariants). Eats ~50% of what Codex would catch, locally, for ~30-60 sec wall-clock. |
| After commit, before PR | **`code-quorum` (four surfaces)** | The standing code-PR review set is **four surfaces** (2026-06-30): **Codex** (GitHub app bot — cross-cutting correctness, hypothesis-driven bug finding, operating-context-aware) plus three local co-primaries run via `local_review.py`: **grok-build**, **grok-composer** (two separate standing xAI surfaces), and **Antigravity** (Gemini 3.x family via the official Google Antigravity SDK, `--engine antigravity`). All four are downstream of the local PRE-REVIEW layer; different model families catch different bug shapes, and `find_divergence.py` logs per-surface findings for cross-model correlation. Full mechanics in `code-quorum` SKILL.md §5.5. Findings → `clawpm issues add`. **Retired:** the Gemini Code Assist GitHub bot + free-tier CLI (sunset 2026-06-24; the Gemini *family* is restored via the Antigravity SDK, not the bot). PR-Agent (descriptive-only) and CodeRabbit (paid, unsubscribed) are not in the pipeline. |
| Before merge (if PR is non-trivial) | **`pr-review-toolkit /review-pr`** | Six specialist agents (silent-failure-hunter, type-design-analyzer, comment-analyzer, pr-test-analyzer, code-reviewer, code-simplifier). Use selectively — silent-failure-hunter for any fix involving `try/except`, type-design-analyzer for new public types, etc. Findings → `clawpm issues add`. |
| Confidence-scored second opinion | **`code-review`** | Multi-agent independent review with confidence scoring to filter false positives. Useful for high-stakes merges where Codex alone isn't enough. |
| Branch cleanup | **`commit-commands /clean_gone`** | After PRs land — reaps branches whose remotes are gone. Worth running after `clawpm done` for tasks that resulted in merged PRs. |
| Cowork session start | **`clawpm-cowork`** | Bootstraps the ephemeral Cowork VM with portfolio repo + clawpm install + context resume. |

### Doctrine: layered review — independent surfaces, different model families

Redundancy comes from **independent surfaces**, not from stacking bots on every PR. Two layers always run: the local PRE-REVIEW layer (Claude + `pr-review-toolkit` specialists) upstream, and a review layer downstream.

The downstream layer's default for code PRs is **four surfaces** (2026-06-30), all local-or-bot and all logged for cross-model correlation:

- **Codex** — the GitHub-app bot (`chatgpt-codex-connector[bot]`); detect its reply via `wait-for-codex.py`, never the comments endpoint.
- **grok-build** and **grok-composer** — two separate standing xAI surfaces, run locally via `local_review.py --engine grok` (read-only, diff embedded in the prompt).
- **Antigravity** — the Gemini 3.x family via the official Google Antigravity SDK, `local_review.py --engine antigravity` (isolated `.venv-antigravity`, read-only, text-only).

Different model families catch different bug shapes; `find_divergence.py` reports per-surface unique/divergent findings so the set can be pruned or escalated over time. Each surface's verdict is a hint — read the body and triage like Codex; none is a sole gate on recovery/ingest/spec-critical merges, which want the operator's eyes.

**Retired:** the Gemini **Code Assist** GitHub bot and its free-tier CLI were sunset 2026-06-24 — do not summon `/gemini review` and do not treat that bot as a parallel primary. The Gemini *family* lives on only through the Antigravity SDK surface above. The earlier "reviewer triangle" assumed paid CodeRabbit + PR-Agent; neither is in the pipeline now (CodeRabbit unsubscribed; PR-Agent deprecated as descriptive-only). Mechanics for all surfaces live in `code-quorum` SKILL.md §5.5.

The "Between commit and push" gate (PRE-REVIEW) is **upstream of the bot layer**. The diff that arrives at Codex already survived a self-review pass, so the bot review shifts from "find bugs" mode to "sanity-check the implementer's stated uncertainties" mode (provided the briefing carries 3-5 named concerns per the code-quorum skill's template requirement). Round-1-clean reviews become the norm.

Skip PRE-REVIEW for ≤50 LOC mechanical changes or pure docs/config — full skip rule lives in `code-quorum` SKILL.md §3. Never skip for auth, serialization, data storage, or any invariant that fails silently.

### How to invoke (ask the operator first)

When suggesting, phrase as a question, not an action:

> "This task has `complexity=l` and pitfalls noted. Want me to run it through the `feature-dev` workflow first? I'll seed the subtasks here in clawpm."

> "Ready to commit? `/commit` will draft the message — I'll add `Closes CLAWP-042` and run `clawpm log commit` after."

> "Before merging, want me to run `pr-review-toolkit /review-pr` with `silent-failure-hunter` and `pr-test-analyzer` enabled? Took us 3 hours of debug last week to catch a swallowed exception."

The operator may decline — record the decline in the task body so future agents see prior context.

### Hookpoint: work_log JSONL

Every clawpm state transition writes a JSON line to `~/clawpm/work_log.jsonl`. Other plugins/skills can subscribe (file-watch or periodic poll) to fire their own logic — e.g., a `pre-commit` review trigger when a task transitions to `progress`. The schema is documented in the README. clawpm doesn't dispatch these subscriptions itself — it stays the data layer.
