---
created: '2026-05-24'
id: CLAWP-024
predictions:
  approach: 'New ''clawpm agent dispatch --prompt PROMPT [--parent TASK_ID] [--rubric-criteria
    CRITERIA]'' command. Internally: 1) auto-create subtask with PROMPT as body and
    CRITERIA as success_criteria, 2) call write_dispatch_settings in a tmp worktree,
    3) subprocess claude --print --settings-file ... (or env-var equivalent), 4) capture
    transcript, run eval-stop on it, 5) write verdict + mark subtask done. Pattern
    matches the existing tasks dispatch path but bundles invocation.'
  complexity: l
  confidence: 2
  duration_min: 420
  files_scope:
  - src/clawpm/agent.py
  - src/clawpm/cli.py
  - tests/test_agent_dispatch.py
  filled_by: agent
  pitfalls: Claude Code's --settings-file flag may not exist or behave as expected.
    Capturing transcript from claude -p subprocess needs care. Worktree creation per
    dispatch is expensive — may need lighter weight per-dispatch dir.
  pre_mortem: 'Most likely failure: Claude Code CLI doesn''t expose a transcript path
    to the subprocess driver. Mitigation: fall back to capturing stdout + a synthetic
    transcript file we build ourselves.'
  predicted_iterations: 3
  success_criteria:
  - criterion: clawpm agent dispatch creates a subtask + runs claude -p + marks done
      with verdict
    gradeable_signal: JSON output shows subtask_id + verdict.ok + reflection event
      written
  - criterion: Subagent transcript captured and evaluated
    gradeable_signal: iteration_event in reflection JSONL with verdict from Stop-hook
      judge
  - criterion: Parent task receives results via inbox
    gradeable_signal: subagent posts to parent inbox with subtask_id + verdict
priority: 5
---
# Agent-tool dispatch bridge: clawpm agent subcommand wraps subagent invocation

Parent-spawned subagents currently bypass clawpm dispatch entirely (Agent tool path). New 'clawpm agent' subcommand wraps subagent invocation: creates a subtask under the current task, dispatches via settings.local.json (CLAWP-018), invokes claude -p, captures verdict via Stop-hook judge (CLAWP-017), marks subtask done. Activates the CLAWP-017+018 infrastructure for the 10x volume path.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

