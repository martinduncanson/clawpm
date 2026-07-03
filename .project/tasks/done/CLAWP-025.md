---
created: '2026-05-24'
id: CLAWP-025
predictions:
  approach: 'New ''clawpm resume'' top-level command. Composes a prompt from: clawpm
    context output + git log -5 + recent work_log entries. Invokes _default_judge_invoker
    (or override via CLAWPM_RESUME_CMD). Returns text. Caches recent rendering to
    ~/clawpm/resume_cache.txt with TTL to keep instant re-runs cheap.'
  complexity: s
  confidence: 4
  duration_min: 120
  files_scope:
  - src/clawpm/cli.py
  - src/clawpm/resume.py
  - tests/test_resume.py
  filled_by: agent
  pitfalls: Same subprocess-claude dependency as eval-stop. May need to gracefully
    degrade when judge unavailable (fall back to raw context output).
  pre_mortem: 'Most likely failure: prompt is too generic and returns boilerplate.
    Mitigation: include 3-5 concrete signal fields in the prompt.'
  predicted_iterations: 1
  success_criteria:
  - criterion: clawpm resume returns 2-paragraph briefing in <5s
    gradeable_signal: elapsed time + paragraph count check
  - criterion: Briefing covers active branch, in-progress task, last commit, next
      likely step
    gradeable_signal: regex spot-check on output
priority: 5
---
# clawpm resume: Claude-rendered 2-paragraph session briefing

Session orientation surface. 'clawpm resume' invokes the same subprocess judge used by Stop-hook (claude --print --model haiku) with a different prompt: feed it work_log tail + active tasks + last 5 git commits, return a 2-paragraph briefing. 'You're on branch X, working on CLAWP-Y. Last commit Z addressed [...]. Next likely step: [...]'.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

