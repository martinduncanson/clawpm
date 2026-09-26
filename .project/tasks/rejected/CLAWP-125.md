---
baseline_ref: 963208e
complexity: m
created: '2026-09-26'
id: CLAWP-125
predictions:
  confidence: 2
  filled_by: agent
  hypothesis: assign_task_prefix's 'safe without a lock' claim covers only same-snapshot
    determinism, not a process acting on a stale computed answer after real portfolio
    state has since changed elsewhere -- if true, two projects can end up minting
    the same literal task id in different project directories, a gap independent of
    and predating CLAWP-121's allocator-algorithm work.
  pre_mortem: 'Most likely failure: treating Codex''s flagged concern as ground truth
    without reproducing it, and either overbuilding a lock for a race that doesn''t
    materialize in practice, or missing that it''s already mitigated by some mechanism
    not considered here.'
  reference_tasks:
  - CLAWP-121
  - CLAWP-051
  success_criteria:
  - Independently verify (or refute) the race with a concrete reproduction BEFORE
    proposing a fix -- this task's first deliverable is confirmation, not a lock implementation
  - 'If confirmed: a fix (portfolio-wide lock, versioned reservation, or documented-acceptable-risk)
    is proposed and reviewed; if refuted: close with the reproduction/reasoning documented'
priority: 4
rationale: 'Duplicate of CLAWP-116 (filed 2026-09-03 from PR #57 round 5, Codex) --
  same defect (no portfolio-wide lock coordinates prefix selection with the task-file
  write), same proposed fix shape (portfolio lock outer, per-project lock inner).
  Should have checked existing tasks before filing; fresh CLAWP-121/PR #60 repro evidence
  attached to CLAWP-116 as a log note instead of duplicating the task.'
supersedes: CLAWP-116
tags:
- task-id-allocation
- concurrency
updated: '2026-09-26'
---
# Investigate TOCTOU race / missing portfolio-wide lock in the prefix allocator

## Scope note — this is a CONCURRENCY issue, separate from the matching-completeness fix

This task is scoped separately from the bipartite-matching correctness fix (see the sibling task filed alongside this one, motivated by PR #60 round 3's Codex counter-example). That task is about the allocator finding a WRONG (or spuriously-refused) answer even in a single-snapshot, no-concurrency read. THIS task is about the answer being computed against a STALE snapshot and then acted on after the real portfolio state has moved — a different failure mode with a different fix (locking/coordination, not a smarter algorithm).

## The concern (Codex, PR #60 round 3 — flagged, not yet independently re-verified)

`assign_task_prefix` / `assign_all_prefixes` (`src/clawpm/tasks.py`) recompute the full portfolio's prefix state fresh on every call (`discover_projects` + `resolve_existing_prefix` + a scan of task-less siblings) with **no portfolio-wide lock**. `add_task`'s actual mint path only takes a PER-PROJECT file lock (`tasks_dir / ".clawpm-tasks.lock"`, CLAWP-051) at write time.

Codex's scenario (paraphrased from the PR #60 round-3 review comment): with task-less `abcdeaa`, `abcdeba`, `abcdebb`, an initial computation assigns `ABCDE`, `ABCDEB`, `ABCDEBB` respectively. If the `abcdeaa` process reads that answer (`ABCDE`) and then PAUSES before actually writing its task file (still holding only its own, as-yet-unacquired, per-project lock), and MEANWHILE `abcdeba` completes its own mint (writes a real task file, so `abcdeba` now has a REAL resolved prefix `ABCDEB`) — then a FRESH process resolving `abcdebb`'s prefix recomputes the WHOLE pass from scratch, sees `abcdeba` as resolved now (not task-less), and can end up assigning `abcdebb` the value `ABCDE` (since nothing has PERSISTED `abcdeaa`'s earlier, still-pending computation of `ABCDE`). If the paused `abcdeaa` process then wakes up and writes based on its STALE computed value (`ABCDE`), and `abcdebb`'s process also writes `ABCDE`, two DIFFERENT projects can end up with task files sharing the literal id `ABCDE-000` — a real cross-project id collision, in two different projects' task directories (so the per-project file lock never contends on the same lock path and never catches it).

## My assessment (needs independent verification, not asserted as certain)

My read, formed while implementing CLAWP-121's rewrite, is that **this predates CLAWP-121 and is a general, already-existing property of the whole `assign_task_prefix` design** — not something either the abandoned reserve-and-patch approach or the shipped deterministic-global-pass rewrite introduced or worsened. The pre-CLAWP-121 `_portfolio_prefixes` helper (on `main`, before this PR) had the identical shape: it also recomputed fresh from `discover_projects`/`resolve_existing_prefix` on every call, with no portfolio-wide lock, and `add_task` has always only locked the individual project's own tasks directory. `assign_task_prefix`'s own docstring has long claimed "concurrent first mints [are] safe WITHOUT a lock" — but on inspection that claim is scoped to "two different projects querying at the same consistent snapshot compute non-colliding answers" (a determinism/purity property), not to "a process that reads a computed answer, pauses, and acts on it after real state has since changed elsewhere" (a genuine TOCTOU window).

**Whoever picks this up should verify this claim independently before treating it as ground truth** — write a reproduction (e.g. a test that monkeypatches/pauses between the compute and write steps of two concurrent `add_task` calls, or a stress test with real threads/processes and injected delays) against `main` BEFORE CLAWP-121 (or against the current `assign_task_prefix`, which has the same architecture) to confirm the race is real and reproducible, not just plausible from reading the code. If it turns out the per-project lock's scope or some other mechanism already prevents this in practice, downgrade or close this task with that finding documented.

## Possible remediation directions (not prescriptive — investigate first)

- A portfolio-wide lock coordinating the "compute candidate" and "first write" steps across ALL task-less projects' `add_task`/`emit_tree` calls, not just per-project.
- OR: persist a stable, versioned "reservation" the moment a candidate is computed (not just the moment it's written), with a short TTL, so a fresh recompute sees in-flight reservations, not just fully-persisted real claims.
- OR: accept the race as low-probability/low-blast-radius (requires two SPECIFIC projects racing during a narrow window, both first-minting) and document it rather than adding coordination overhead — a legitimate outcome if the investigation above finds the practical risk is low.

## References

- PR #60: https://github.com/martinduncanson/clawpm/pull/60 (merged as 963208e) — round-3 Codex review comment is the source of this concern.
- CLAWP-121 (`.project/tasks/done/CLAWP-121.md`) and CLAWP-051 (per-project file locking) for related history.


## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

