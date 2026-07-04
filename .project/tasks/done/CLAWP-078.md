---
baseline_ref: 0b307f2
complexity: l
created: '2026-07-03'
id: CLAWP-078
predictions:
  confidence: 3
  duration_min: 300
  filled_by: agent
  pre_mortem: 'Most likely failure: settings.toml pause/resume string-replace has
    no clean core function to route through and needs a proper TOML writer, growing
    scope; timebox and split if so'
  success_criteria:
  - 'Decision recorded: harden (full parity) vs demote (read-only prototype)'
  - test_serve.py exists using FastAPI TestClient covering every route incl. error
    paths
  - All handlers return proper HTTP status codes (400/404/409/500) with a consistent
    envelope
  - Mutating routes call the same core functions as the CLI (no bypass writers); web
    create_issue no longer hand-rolls issues.jsonl
  - fastapi+uvicorn moved to an optional [web] extra with graceful ImportError message
    on clawpm serve
priority: 5
scope:
- src/clawpm/serve.py
- src/clawpm/web/
- tests/test_serve.py
- pyproject.toml
---
# serve.py web layer: harden or demote to read-only

Audit 2026-07-03: serve.py (12.5K, FastAPI) has ZERO tests yet MUTATES state. Specific defects: every handler returns 200 with errors in-band via broad except Exception (serve.py:90-91,159-160), envelope inconsistent (bare error keys at 84,100); create_issue bypasses the CLI issue writer with its own hardcoded .agent/issues.jsonl schema (302-319) - drift risk; pause/resume naive string-replace on settings.toml (225/244) breaks on formatting variance; web create_task (251) accepts no predictions/success-criteria - an unguarded side door around the calibration discipline the CLI enforces. Positive: respond route already uses per-project file_lock + atomic tmp-replace; binds 127.0.0.1.

SPEC: first DECIDE harden vs demote. Recommendation: demote to read-only NOW (delete or 405 the mutating routes) unless the web UI has real usage, then harden incrementally behind tests. Either way: TestClient suite, status codes, single envelope, route any surviving writes through core functions, [web] extra. ALSO: unconditional fastapi/uvicorn deps (pyproject.toml:11-19) move to [project.optional-dependencies] web.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

