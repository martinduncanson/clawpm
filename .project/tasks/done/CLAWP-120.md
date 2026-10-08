---
baseline_ref: a5f06ab
created: '2026-09-17'
id: CLAWP-120
predictions:
  complexity: m
  duration_min: 90
  filled_by: agent
priority: 2
updated: '2026-09-19'
---
# Fix CLAWP-119 digest-fallback-removal fallout: doctor swallows allocator refusal + 2 broken tests + portfolio_prefixes over-eager



## Acceptance Criteria

- [x] `project_doctor`'s cross-project prefix-collision check (`project.py`)
  reports an allocator refusal (CLAWP-119 `ValueError`) as an `issues[]`
  warning WITHOUT also inserting the project into `prefix_map` under its
  naive base — that fallback reproduced the exact false-collision bug
  already fixed for the resolved-prefix path one block above.
- [x] The same check's exception handling also catches `OSError` (a
  sibling's, or the project's own, task dir failing to scan) rather than
  letting it propagate and abort `doctor` for the whole portfolio — mirrors
  the lease-scanning block's `except Exception` -> issues[] pattern.
- [x] `test_doctor_surfaces_allocator_refusal_instead_of_swallowing_it`
  updated: previously pinned the false-collision entry as expected output;
  now asserts it is ABSENT while the `issues[]` warning is still present.
- [x] New regression test
  `test_doctor_surfaces_unreadable_sibling_task_dir_instead_of_aborting`
  added, patching `resolve_existing_prefix` (not the filesystem) to isolate
  this fix's own code path from an unrelated pre-existing unguarded
  `Path.iterdir()` in `list_tasks`/`_scan_task_files` (`project.py:508`) —
  that gap is real but out of scope here, tracked separately as CLAWP-094.
- [x] Full suite green (1551 passed) after the fix.
- [ ] `_portfolio_prefixes`' `len(id) > 5` extend-room proxy (Open thread #2
  from the 1033cd33 baton manifest) — pre-existing, non-regression gap,
  not fixed this round; still open, lower priority.
- [ ] PR #57 code-quorum BRIEF step (Codex + grok-4.6/grok-4.5/antigravity)
  — not yet run this round; do after committing this fix.

## Notes

**2026-09-18, this session (resumed from 1033cd33 baton):** The prior
session's PRE-REVIEW pass (baseline-reviewer + silent-failure-hunter
subagents, run against `a3baf52` before briefing code-quorum) found two
real bugs in the just-pushed refusal-arm handling, both now fixed together
in `project.py`'s `except (ValueError, OSError)` block:

1. **False-collision fallback removed.** The refusal arm was falling back
   to `_naive_prefix(proj.id)` and inserting into `prefix_map` "so the
   project still appears in the map" (a round-6 decision from PR #57).
   Verified `prefix_map` has no reader besides `prefix_collisions` — so
   nothing needed it to "still appear", and the fallback manufactured a
   collision between a project that will never mint that prefix and a
   sibling whose prefix is perfectly valid. Fixed by `continue`-ing past
   the `prefix_map.setdefault(...)` insertion on refusal.
2. **Except clause widened to `(ValueError, OSError)`,** and restructured
   so BOTH `_resolve_prefix(proj)` (the project's own resolution) AND
   `_assign_prefix(...)` (which internally scans every sibling via
   `_portfolio_prefixes`) share one try/except — the original diagnosis
   only mentioned the sibling-scan path inside `_assign_prefix`, but
   `_resolve_prefix(proj)` at the top of the loop hits the identical
   unguarded `_infer_prefix_from_tasks` -> `Path.iterdir()` call for the
   CURRENT project's own directory, discovered while writing the
   regression test (an "own turn" locked-directory case would have
   crashed `doctor` even with the narrower fix).

Re-verified both fixes against the actual allocator functions (not the
subagents' suggested code verbatim, per the baton's own caveat) before
applying. Did not touch Open thread #2 (`len(id) > 5` proxy) — pre-existing,
not a regression, lower priority; deferred.

