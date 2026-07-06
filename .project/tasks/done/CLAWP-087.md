---
baseline_ref: 0b307f2
complexity: s
created: '2026-07-03'
id: CLAWP-087
predictions:
  confidence: 4
  duration_min: 45
  filled_by: agent
  success_criteria:
  - research add template no longer emits placeholder Summary/Findings/Conclusion
    stubs that rot, OR research add gains --summary/--verdict flags that populate
    them directly
  - A doctor (or research list) signal flags entries older than N days still carrying
    placeholder sections
  - 'The four existing entries are retrofitted: verdicts moved out of the Question
    field into Summary'
priority: 6
scope:
- src/clawpm/research.py
- src/clawpm/cli.py
updated: '2026-07-06'
---
# Fix research entry template/workflow mismatch (verdict-in-Question antipattern)

Dogfooding finding 2026-07-03: all four .project/research/ entries have Summary/Findings/Conclusion as literal (To be filled in) stubs while the real verdict is crammed into the Question frontmatter/section - the template does not match how research is actually captured (single-shot verdict at creation time, not progressive fill-in). The schema fights the workflow; the workflow wins; the schema rots.

SPEC: make the template match reality - research add --summary "..." (and optionally --finding repeatable) writing straight into the sections; keep progressive sections for genuinely open investigations via --open. Cheap, removes a standing embarrassment for any agent reading research entries.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

