---
baseline_ref: ts:2026-07-04T08:38:56.888662+00:00
created: '2026-07-04'
id: CLAWP-089
predictions:
  complexity: m
  confidence: 4
  duration_min: 120
  filled_by: operator
priority: 5
rationale: 'Duplicate filing of the same finding CLAWP-087 already covers (identical
  title/body: research entry template verdict-in-Question antipattern). CLAWP-087
  did the actual work and is done (PR #43, merged 2026-07-06). This is a stale duplicate
  from the cross-worktree next-id race (see CLAWP-092), not a separate work item.'
updated: '2026-07-10'
---
# Fix research entry template/workflow mismatch (verdict-in-Question antipattern)

Dogfooding finding 2026-07-03: all four .project/research/ entries have Summary/Findings/Conclusion as literal (To be filled in) stubs while the real verdict is crammed into the Question frontmatter/section. The template does not match how research is actually captured (single-shot verdict at creation time, not progressive fill-in). Schema fights workflow; workflow wins; schema rots.

SPEC: make template match reality. research add --summary \"...\" (and optionally --finding repeatable) writing straight into sections. Keep progressive sections for genuinely open investigations via --open. This is cheap and removes standing embarrassment for any agent reading research entries.

Success criteria:
1. research add template no longer emits placeholder Summary/Findings/Conclusion stubs that rot, OR research add gains --summary/--verdict flags that populate them directly
2. A doctor (or research list) signal flags entries older than N days still carrying placeholder sections
3. The four existing entries are retrofitted: verdicts moved out of the Question field into Summary

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

