---
baseline_ref: 85fb446
complexity: m
created: '2026-09-27'
id: CLAWP-130
predictions:
  confidence: 2
  filled_by: agent
priority: 4
updated: '2026-09-27'
---
# Explicit-ID create can squat a prefix a taskless sibling later auto-mints into

Follow-up from CLAWP-129's review (PR #66, PRE-REVIEW subagent finding #3,
confidence 82). `check_explicit_id_prefix_collision` (CLAWP-129) only
refuses an explicit id whose prefix matches another project's CURRENT real
claim (`resolve_existing_prefix`: explicit `task_prefix` or the DOMINANT
inferred prefix). It does not consult `assign_all_prefixes`' full
candidate-assignment map, which also covers every currently-taskless
project's deterministic future first mint.

Concretely: project A explicitly creates `--id BRAVO-900` (allowed —
nothing currently claims `BRAVO`). Project A's own resolved prefix is
unaffected (still whatever its dominant inferred prefix is), so `BRAVO`
never enters `assign_all_prefixes`' `used` set. A still-taskless
`bravo-project` later runs its first auto-mint, `assign_all_prefixes`
deterministically assigns it `BRAVO` (nothing has claimed it), and it
mints `BRAVO-000` — colliding with project A's pre-existing `BRAVO-900`.
This is the exact cross-project id-space collision class CLAWP-129 was
filed to prevent, just deferred by one create instead of prevented.

**This needs an operator design decision, not a mechanical fix** — the
litmus from `code-quorum`'s action taxonomy: resolving it means choosing
between defensible behaviours, not applying an obvious correction.
Options, roughly in order of invasiveness:

1. **Leave as-is.** This is a narrow edge case (an operator explicitly
   picking a prefix unrelated to their own project's naming, which later
   happens to be a DIFFERENT taskless project's natural candidate) and
   CLAWP-129's own acceptance criteria already explicitly scoped "let
   through when nothing currently claims it" as correct behaviour,
   matching the CLAWP-116 precedent for a project's own first mint.
2. **Widen the check to call `assign_all_prefixes(config)` and compare
   against its full returned assignment map**, not just
   `resolve_existing_prefix` per sibling. Closes the gap, but changes the
   allow-through semantics fairly fundamentally: it would mean an
   explicit-ID create can be refused by a prefix NO project currently
   claims, only because the deterministic allocator would someday assign
   it to some other still-taskless project. Needs its own test matrix
   (does `test_explicit_id_for_own_first_mint_is_allowed_even_if_unclaimed`
   still pass? Almost certainly yes since proj-a's own candidate is
   unrelated to a colliding id — but needs verifying, not assuming).
3. **Something narrower**: e.g. only widen the check when the explicit id
   is being created for a currently-taskless project itself (so its own
   future first-mint conflict is caught), leaving cross-project squats on
   an unrelated prefix out of scope as accepted risk.

## Acceptance Criteria

- [ ] Operator picks one of the above (or another option) and records the
  decision here before implementation starts.
- [ ] Whichever option is chosen, add a regression test reproducing the
  `BRAVO-900` / `bravo-project` scenario above.

## Notes

Not a regression introduced by CLAWP-129 — it's a residual property of the
portfolio's prefix-allocation design (an explicit id was never checked
against ANY future allocation before CLAWP-129 either; CLAWP-129 only
closed the "colliding with a claim that already exists" half of the
invariant).

