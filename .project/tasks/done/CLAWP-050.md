---
complexity: m
created: '2026-06-10'
id: CLAWP-050
predictions:
  approach: 'Make clawpm actively steer: commands return a terse optional next-action
    hint. tasks add -> if body suggests independent sub-pieces, hint decompose. next
    -> if task has parallel_group siblings, hint --batch. tasks show with success_criteria
    -> hint dispatch/subagent-judge. Terse, in a dedicated hints field of the JSON
    (and one greyed line in text mode). Opt-out via --no-hints flag and/or CLAWPM_NO_HINTS
    env. Hints are heuristic and code-derived (deterministic-first), never an LLM
    call.'
  confidence: 3
  duration_min: 300
  files_scope:
  - src/clawpm/cli.py
  - src/clawpm/output.py
  filled_by: agent
  success_criteria:
  - at least 3 command paths emit a context-appropriate next-action hint in a structured
    hints field
  - hints suppressible via --no-hints / CLAWPM_NO_HINTS; off-path commands emit none;
    full suite green
  unknowns: Whether hints belong in stdout JSON (machine-read) or stderr (human-only)
    - leaning a structured 'hints' key so agents can read them
priority: 5
---
# Runtime next-action hints: clawpm commands nudge the agent to the right next move



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

