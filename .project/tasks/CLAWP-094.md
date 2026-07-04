---
baseline_ref: ae2101f
created: '2026-07-04'
id: CLAWP-094
predictions:
  approach: 'Grok (co-primary) surfaced during CLAWP-081 review: bare except Exception
    swallowing in detect_project_from_cwd/get_context_project/list_research/get_research;
    run_apply_phase appending skipped/error dicts into applied[]; stale-blocked cascade
    leaving stale state: frontmatter; load_portfolio_config fail-open default. Coherent
    pre-existing issue across context.py/research.py/doctor_apply.py/discovery.py,
    out of scope for the CLAWP-081 tests-only PR.'
  complexity: m
  confidence: 3
  duration_min: 180
  filled_by: agent
  success_criteria:
  - 'Each identified fail-open site either surfaces its error explicitly or has documented
    rationale for staying fail-open; stale-blocked cascade no longer leaves stale
    state: frontmatter; tests cover each fixed site'
priority: 5
---
# Harden fail-open error handling + config-default contract (discovery/context/research/doctor surface)



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

