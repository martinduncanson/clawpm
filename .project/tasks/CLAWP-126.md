---
baseline_ref: 97e509e
complexity: m
created: '2026-09-26'
id: CLAWP-126
predictions:
  confidence: 3
  filled_by: agent
  hypothesis: If the updated frontmatter writer and doctor's stale-blocked reader
    agree on a single timezone convention (both UTC, or a full timestamp instead of
    a date), then the false-positive (negative-UTC-offset) and delayed-detection (positive-UTC-offset)
    mismatches both disappear, without reintroducing CLAWP-123's flake in the other
    direction.
  pre_mortem: 'Most likely failure: treating this as a drop-in one-line writer change
    without auditing every OTHER reader of the updated stamp (not just doctor''s stale-blocked
    check) for an implicit local-date assumption.'
  reference_tasks:
  - CLAWP-123
  - CLAWP-086
  success_criteria:
  - A test using the REAL writer path (change_task_state or equivalent) confirms doctor's
    stale-blocked reader interprets the resulting stamp within a bounded, timezone-independent
    window of the actual block time
  - Full suite green including all ~17 test_updated_timestamp.py assertion sites pinning
    date.today() writer behaviour, updated to match whatever convention is chosen
priority: 5
tags:
- testing
- flaky
updated: '2026-09-26'
---
# updated stamp: writer stores LOCAL date, doctor's stale-blocked reader interprets UTC end-of-day

## The mismatch (PRE-REVIEW, PR #61, code-reviewer subagent)

Every WRITER of the `updated` frontmatter stamp uses the LOCAL calendar date:
- `src/clawpm/frontmatter.py:74` — `frontmatter["updated"] = when or date.today().isoformat()`
- `src/clawpm/tasks.py:554` — `_set_updated_line(text, when or date.today().isoformat())`

But the ONE reader that interprets this date-only stamp for a time-sensitive check (doctor's stale-blocked cutoff, `src/clawpm/cli/project.py:583-594`) reads it as END-OF-DAY UTC on that date:

```python
_bd = date.fromisoformat(str(bt.updated).strip())
btime = datetime(_bd.year, _bd.month, _bd.day, 23, 59, 59, tzinfo=timezone.utc)
```

This is a genuine writer/reader disagreement, not just a test artifact (CLAWP-123, which fixed the TEST that exercises this reader, but left the underlying product mismatch in place, deliberately out of scope for that fix).

## Concrete impact on a non-UTC host

- **Negative UTC offset** (most of the Americas, e.g. UTC-8): a task blocked at local day D, 20:00 (= UTC day D+1, 04:00) is stamped `updated: D`. The reader treats that as `D 23:59:59Z`. Doctor flags it stale ~16 hours after the real block, not 24 — a **false positive** against the reader's own stated intent (CLAWP-086's comment: "so a task blocked late on day D isn't falsely reported the next morning"). The mechanism this comment was written to prevent still fires for negative-offset machines.
- **Positive UTC offset** (e.g. UTC+13/+14): a block shortly after local midnight is stamped `D+1` while UTC is still on `D`. The reader's interpretation can then be up to ~48h in the future relative to the actual block time — stale-blocked detection is delayed by up to a full extra day.

## Why this wasn't fixed inside CLAWP-123

Fixing the WRITER (stamp in UTC instead of local date, or store a full timestamp instead of a date) is a product behaviour change, not a test fix, and touches broadly:
- `tests/test_updated_timestamp.py` pins the local-date writer behaviour throughout (`assert ... == date.today().isoformat()` at lines 108, 126, 139, 150, 160, 173, 188, 199, 220, 248, 264, 266, 294, 353, 366, 391, 429 — roughly 17 assertion sites).
- Any change to the writer needs those tests updated in lockstep, plus a decision on migration for ALREADY-WRITTEN stamps on disk (same numbering-continuity question CLAWP-113 already raises for a different legacy-prefix migration).

## Recommended fix shape (investigate before committing to one)

- **Option A**: writer stamps `datetime.now(timezone.utc).date().isoformat()` instead of `date.today().isoformat()`. Simplest, but changes the semantic meaning of `updated` for every OTHER reader too (anything that currently treats it as "the local day this happened" — audit for other readers before assuming this is side-effect-free).
- **Option B**: writer stores a full UTC timestamp (not date-only) for `updated`, and update the stale-blocked reader (and any other consumer) to parse a timestamp instead of reinterpreting a date. Bigger diff, but removes the whole class of date-only ambiguity permanently.
- **Option C**: leave the writer as local-date, and make ONLY the stale-blocked reader's interpretation timezone-aware relative to what "local" meant at write time (would need to also store an offset or use a full timestamp anyway — likely converges to Option B).

## Test plan

- A test that stamps `updated` via the REAL writer path (`change_task_state` or equivalent), then verifies doctor's stale-blocked reader interprets it as within some bounded, timezone-independent window of the ACTUAL block time (not just "close enough on this test machine's clock") — this is the test CLAWP-123 could NOT write, because it would have required changing the writer, which was out of scope there.
- Full suite green after whatever writer-side change is chosen, including the ~17 `test_updated_timestamp.py` sites above updated to match.

## References

- PR #61 (CLAWP-123): https://github.com/martinduncanson/clawpm/pull/61 — PRE-REVIEW subagent's Finding 1, which named this as a real bug but correctly out of scope for that PR (test-only fix).
- `src/clawpm/cli/project.py:583-594` (the reader), `src/clawpm/frontmatter.py:74` + `src/clawpm/tasks.py:554` (the writers), CLAWP-086 (the comment explaining the reader's original UTC-end-of-day design intent).


## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

