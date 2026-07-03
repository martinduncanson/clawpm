---
complexity: m
created: '2026-05-13'
id: CLAWP-000
predictions:
  approach: 'Add 2 new checks to project_doctor: (d) commit-drift via git log --since=<last_log_ts>
    wc -l + threshold from settings.toml or --commits-drift-threshold; (e) marker
    presence in CLAUDE.md|AGENTS.md|README.md. New command ''clawpm project announce''
    picks target file by precedence, replaces or appends a stanza bounded by <!--
    clawpm:project-requirement --> ... <!-- /clawpm:project-requirement --> markers.
    project init auto-runs announce after settings.toml write.'
  complexity: m
  confidence: 4
  duration_min: 120
  filled_by: agent
  pre_mortem: 'False-positive drift on shallow clones (depth limits git log). Mitigation:
    skip check if shallow clone detected. False-positive marker check on repos with
    existing ''clawpm'' mentions in docs that aren''t the canonical marker — mitigate
    by checking for the explicit HTML comment, not the word ''clawpm''.'
  reference_tasks:
  - CLAWPM-001
  success_criteria:
  - doctor warns when project HEAD has >threshold commits authored after last work_log
    entry; doctor warns when no CLAUDE.md/AGENTS.md/README.md contains the clawpm-requirement
    marker; project announce command writes/replaces marker block idempotently; project
    init auto-runs announce; 5+ new tests green; full suite 220+ tests green
  unknowns: (1) Per-project threshold override mechanism — settings.toml or CLI flag?
    Prefer settings.toml. (2) Whether announce should ever create CLAUDE.md from scratch
    or only modify existing files — default to create-if-missing since beaconize had
    no CLAUDE.md. (3) What to do when work_log has zero entries for a project — treat
    as 'never logged' = always warn? Or skip the check?
priority: 2
---
# Phase 1.8: doctor commit-drift check + project announce command



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

