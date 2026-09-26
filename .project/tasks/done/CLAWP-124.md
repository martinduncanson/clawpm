---
baseline_ref: 963208e
complexity: m
created: '2026-09-26'
id: CLAWP-124
predictions:
  confidence: 3
  filled_by: agent
  hypothesis: If assign_all_prefixes uses a real augmenting-path bipartite matcher
    (Kuhn's algorithm) instead of greedy most-constrained-first, then the abcde-c/abcdeb-/abcdeb--
    class of spurious refusal (a valid assignment exists but greedy can't find it)
    is eliminated, because augmenting-path matching is provably complete for this
    problem.
  pre_mortem: 'Most likely failure: an augmenting-path implementation that isn''t
    itself deterministic (traversal/tie-break order not fixed), reintroducing a NEW
    cross-process non-determinism bug of the exact shape CLAWP-121 round 1 already
    fixed once.'
  reference_tasks:
  - CLAWP-121
  success_criteria:
  - New regression test using the abcde-c/abcdeb-/abcdeb-- reach-tie topology (currently
    fails against the shipped greedy allocator) passes after the fix
  - Full test suite passes with no regression in the 6 existing TestDeterministicGlobalPrefixPass
    tests or any other prefix-allocation test
priority: 3
tags:
- task-id-allocation
- concurrency
updated: '2026-09-26'
---
# Replace greedy most-constrained-first with real bipartite matching in assign_all_prefixes (correctness fix)

## This is a correctness fix, not a nice-to-have refactor

`assign_all_prefixes` / `_assign_taskless_prefixes` (`src/clawpm/tasks.py`, shipped in CLAWP-121 / PR #60, merged 963208e) uses a greedy **most-constrained-first** heuristic: at every step, pick the task-less project id with the fewest currently-free id-derived candidates (`len(_naive_prefix_reach(pid) - used)`, recomputed dynamically at each pick) and mint it its shortest free candidate.

This greedy heuristic is **provably incomplete** for the general matching problem it's solving. It is not merely a suboptimal choice in some edge case — it can and does spuriously refuse a project (`ValueError`, "every id-derived candidate is claimed") even when a valid, collision-free assignment exists for the whole portfolio. Do not deprioritise this as polish: it is a real correctness gap in the shipped allocator, confirmed with a live, reproducible counter-example (below), independently found by two review surfaces across the CLAWP-121 rewrite's 3 review rounds (Codex found it directly; grok-4.5 initially claimed the opposite — that the nested-candidate-chain structure made this incompleteness class "unreachable" — and was wrong, refuted by the same counter-example).

## The counter-example (Codex, PR #60 round 3, verified live against the shipped code)

Three task-less project ids, no real (explicit/inferred) claims involved:
- `abcde-c` — reach (distinct id-derived candidates): `{ABCDE, ABCDE-C}` (2)
- `abcdeb-` — reach: `{ABCDE, ABCDEB}` (2)
- `abcdeb--` — reach: `{ABCDE, ABCDEB}` (2, IDENTICAL to `abcdeb-`'s — its only non-base candidate also collapses to `ABCDEB` via trailing-separator stripping)

All three tie at reach=2 initially. The deterministic tiebreak (`pid.upper()`, then `pid`) processes `abcde-c` first. It greedily takes its shortest candidate, `ABCDE` — even though it has a perfectly fine alternative (`ABCDE-C`). That leaves `abcdeb-` and `abcdeb--` to split `{ABCDE, ABCDEB}` with `ABCDE` already gone: one of them takes `ABCDEB`, the other has nothing left and raises.

But a valid assignment for all three exists: `abcde-c -> ABCDE-C`, and the two `abcdeb*` ids split `{ABCDE, ABCDEB}` between them. The greedy algorithm can't find it because it never reconsiders an earlier pick once made — reassigning `abcde-c` from `ABCDE` to `ABCDE-C` would free up the slot the third project needs, but nothing in the current algorithm ever does that.

Verified live (2026-09-25) via:
```python
from clawpm.tasks import _assign_taskless_prefixes
assignments, errors = _assign_taskless_prefixes({"abcde-c", "abcdeb-", "abcdeb--"}, set())
# assignments == {'abcde-c': 'ABCDE', 'abcdeb-': 'ABCDEB'}
# errors == {'abcdeb--': ValueError(...)}  <- spurious refusal
```

## Why this needs real matching, not another ordering tweak

CLAWP-121's original PR #60 spent 4 review rounds patching a "reserve a sibling's candidate chain" approach — each patch closed one bug shape and exposed another. The redesign (deterministic global pass, this same PR under a new approach) was meant to end that pattern by never reserving a *prediction* — but it still needed 2 more fix rounds after the initial rewrite (round 1: alphabetical sort didn't track real flexibility; round 2: static total-reach-count sort didn't account for real claims consuming a sibling's options unevenly), and round 3 found this: greedy MCF/MRV ordering, however well-tuned the tiebreak, is fundamentally the wrong SHAPE of algorithm for a problem that sometimes requires reassigning an earlier decision. This is why the task was paused rather than patched a 4th time (see PR #60 thread and CLAWP-121's own history for the full round-by-round trace).

The correct fix is a real bipartite-matching algorithm — e.g. **Kuhn's algorithm** (augmenting-path search): when a project's greedy-preferred candidate is already taken, attempt to recursively reassign the CURRENT holder of that candidate to one of ITS OWN other free candidates, and only fail if no augmenting path exists anywhere in the graph. This is a small, well-understood, textbook algorithm (not novel design), and it is *provably complete* for this problem (a valid assignment is found whenever one exists, per König's theorem / Hall's theorem for bipartite graphs) — unlike another greedy heuristic, which the last 3 review rounds have shown will likely just find a new counter-example.

## Structural notes for whoever picks this up

- Left nodes: task-less project ids. Right nodes: id-derived candidate strings. Edges: `pid`'s candidate chain (`_naive_prefix_candidates`, shortest-first preference order). Real claims (explicit `task_prefix` / inferred) are FIXED pre-assignments, never part of the matching search space — they just occupy right-nodes permanently.
- Each id's own candidate chain is a totally-ordered ("staircase"/nested) sequence — this special structure is why some naive analyses (mine included, during CLAWP-121's implementation) initially believed greedy-with-the-right-tiebreak would be complete. It isn't; the counter-example above is real and reproducible.
- Preserve the existing invariant that the RESULT is deterministic given the same portfolio state (no dependence on set/dict iteration order or hash seed) — augmenting-path search needs its own fixed traversal order (e.g. always try a project's OWN candidates in shortest-first order, and always explore reassignment candidates in a fixed deterministic order too).
- Keep `PortfolioPrefixScanError`'s existing fail-closed behaviour for sibling scan failures (OSError) — that's orthogonal to the matching algorithm itself.
- All 6 regression tests added across CLAWP-121's rewrite (`tests/test_task_id_allocation.py::TestDeterministicGlobalPrefixPass`) must continue passing; ADD this exact `abcde-c`/`abcdeb-`/`abcdeb--` scenario as a new regression test that currently fails and must pass once this ships.
- Full suite baseline at merge time: 1685 passed (`git show 963208e`).

## References

- PR #60: https://github.com/martinduncanson/clawpm/pull/60 (merged as 963208e) — full round-by-round history of both the abandoned reserve-and-patch approach (rounds 1-4) and the deterministic-global-pass rewrite (3 further rounds) is in the PR thread.
- Commits: 54e0e55 (rewrite), 5bc4b0b (round-1 fix), 73a9af6 (round-2 fix, current shipped state).
- CLAWP-121 (`.project/tasks/done/CLAWP-121.md`) for the original bug and the abandoned approach's history.


## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

