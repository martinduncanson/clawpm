---
baseline_ref: 1f4316f
created: '2026-09-03'
id: CLAWP-116
predictions:
  complexity: l
  confidence: 3
  duration_min: 180
  filled_by: agent
  success_criteria:
  - A portfolio-scoped lock serializes first-mint prefix allocation through the task-file
    write, with a regression test that concurrently allocates for two task-less near-twin
    projects and asserts distinct prefixes, plus the lock-ordering invariant documented
    at both lock sites
priority: 5
updated: '2026-09-26'
---
# Coordinate first-mint prefix allocation with the task-file write

Codex P1 on PR #57 round 5 (thread PRRT_kwDOSVLYYc6ezgmF, tasks.py:1351). assign_task_prefix's EXTENSION arm is non-injective: verified locally that code-quorum, code-quorumx, code-qu and code-quorum-two all derive CODE-Q at n=6. Two task-less near-twin projects minting their FIRST task concurrently each see only the other's normalized BASE placeholder (CODE) in the used set - never the other's selected extension - so both select CODE-Q and add_task's per-project locks let both write CODE-Q-000. Note Codex's specific example in that thread is wrong (code-quorum gives CODE-Q, code_quorum gives CODE_Q - they differ), but the general claim is right and reproduces with the ids above. SCOPE NOTE: the non-injectivity of the extension arm predates CLAWP-096, and so does the actual defect - there is no coordination between prefix selection and the task-file write that reserves it. CLAWP-096's trailing-separator strip does add some new collision cases (codeq- now yields CODEQ at n=6, which can meet codeq's base). PROPOSED FIX, which is what Codex asked for in both round-5 threads: hold a portfolio-scoped file_lock across BOTH prefix selection and the task-file write, acquired only on a first mint (when _infer_prefix_from_tasks returns None) so ordinary task creation stays parallel across projects. Lock ordering invariant to establish and document: portfolio lock OUTER, per-project tasks lock INNER, always - nothing takes the portfolio lock today so there is no inversion risk yet, but that must not be introduced later. This is a change to the concurrency model of the core write path, which is why it is split out of PR #57 rather than appended to it. Related: the 128-bit digest fallback shipped on PR #57 is injective only to a cryptographic bound; this task is what would make it provably unnecessary.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

