---
baseline_ref: c7e9b5d
created: '2026-10-16'
id: CLAWP-162
priority: 7
updated: '2026-10-16'
---
# Resolve-guard design: non-strict Path.resolve() on Windows swallows winerror 3/5, so identity sites (sessions.canonical_path, #127/#129) do not refuse on a missing/unavailable target; decide strict vs exists-check, then guard the remaining unchanged resolves (tasks.py:1400,1498,2218,3128; research.py:243; discovery.py:659,730; sessions.py:798; context.py; land.py)



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

