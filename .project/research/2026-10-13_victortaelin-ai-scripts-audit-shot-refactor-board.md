---
created: '2026-10-13'
id: clawpm-research-victortaelin-ai-scripts-audit-shot-refactor-board
status: open
tags:
- prior-art
- dispatch
- ship-loop
type: investigation
---
# VictorTaelin/AI-scripts audit: shot, refactor, board, long

## Question

Does VictorTaelin/AI-scripts (shot, refactor, board, long) contain anything worth adapting into clawpm?

## Summary

Narrow yes. Only long (codex loop) maps onto clawpm: bounded rewrite-in-place MEMORY, async QUESTIONS/ANSWERS channel, mandatory commit-per-round as baton. board is a code-quorum concern, refactor and shot are editor internals clawpm does not need. No licence on the repo (GitHub reports none): adopt ideas, never copy code. long also runs codex with --dangerously-bypass-approvals-and-sandbox, git add -A and no worktree isolation, which is the failure mode CLAWP-147 exists to prevent.

## Findings

- long: .long/MEMORY is a rewrite-in-place notebook with a hard token cap (2048), explicitly NOT append-only. clawpm progress and worklog are append-only. Candidate: capped per-task memory for dispatched workers.
- long: .long/QUESTIONS plus ANSWERS is an async worker-to-human channel; the worker keeps working, answers are injected next round then cleared. Candidate: check whether clawpm inbox can carry a question kind with full-context rule.
- long: every round MUST end in a commit even when nothing changed; bad attempt = git stash plus empty commit; message ~256 tokens (what, why, results, next). The commit is the baton. Candidate: adopt as a convention in the P1 ship-loop playbook (CLAWP-147-005), zero code.
- long: loop terminates on a self-reported HALT tag. Weaker than the clawpm rubric plus Stop-hook judge. Do not adopt.
- board: parallel multi-vendor advisors, per-advisor ok/failed status in the output header, exits nonzero only if ALL fail. Good degradation shape, but it belongs in code-quorum, not clawpm.
- refactor: cheap model emits an omit list over numbered blocks, only when imports exist and context >= 32k tokens. clawpm already has semble and codegraph retrieval, so low value. Its per-file line-ending preservation echoes the CRLF/BOM lessons (CLAWP-146).
- shot: str_replace must match exactly one location or be refused; fallback match ignores only trailing whitespace; mixed command kinds on one file skip that file. Principle (unique anchor or refuse) is relevant to the edit_fog splice bug in CLAWP-146; no code to adopt.

## Conclusion

Adopt three long ideas as small follow-ups (capped task memory, question channel, commit-as-baton convention). Route board to code-quorum. Skip refactor and shot. Verified by reading Board.ts, Long.ts, Refactor.ts, Shot.ts in full; DeepWiki was stale (described chatsh/holefill) and AskAI.ts was not read. clawpm inbox semantics were NOT verified.
