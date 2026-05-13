# clawpm

Multi-project task and research management — the persistent state substrate for cross-session, cross-agent work.

<!-- clawpm:project-requirement -->
## Project management — clawpm

This project (clawpm) uses [clawpm](https://github.com/martinduncanson/clawpm) for task tracking and calibration capture. Any agent working in this repo MUST use the existing PM structure rather than improvising:

- `clawpm tasks list --project clawpm` — see open tasks before starting work.
- `clawpm tasks add --project clawpm --predict-*` — file new work with predictions (duration, complexity, success-criteria, pre-mortem). Use `--predicted-by agent` and ask the operator to confirm.
- `clawpm log` after substantive work; `clawpm log commit` after each commit to populate the work_log.
- `.project/SPEC.md` is the project scope; `.project/tasks/` is the live backlog; `.project/notes/` holds operator-facing notes (read these before starting).

If `clawpm` is not on PATH: `pipx install git+https://github.com/martinduncanson/clawpm` (then `clawpm doctor` to verify).
<!-- /clawpm:project-requirement -->
