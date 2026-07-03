---
created: '2026-05-22'
id: CLAWP-015
predictions:
  approach: Parallel Explore subagents per repo using repo-research bin/ (DeepWiki
    + gh api + selected reads). Synthesise comparison. Read goals-short/long.txt +
    claude /goal docs. Map patterns to clawpm primitives. Rank by adoption fit.
  complexity: m
  confidence: 3
  duration_min: 90
  filled_by: agent
  pitfalls: DeepWiki misses on small/new repos; some repos may be empty/abandoned;
    goals patterns may overfit to Hermes-specific dashboard
  pre_mortem: 'Most likely failure: shallow per-repo summaries that miss the novel
    mechanism. Mitigation: read SKILL.md/README + the central orchestration file for
    any repo that looks interesting.'
  success_criteria:
  - Clawpm research entry filed via clawpm research add
  - 3-5 ranked feature candidates with adoption sketch each
  - Goals primitive (/goal + Mission Control) explicitly evaluated for clawpm relevance
  unknowns: Whether any of these have a primitive better than clawpm's reflection-events
    JSONL + scope-aware dispatch combo
priority: 5
---
# Research adjacent task/agent frameworks + Claude /goal for clawpm feature mining

Trawl 8 repos (swarma, malphas/ralph, guild, task-magic, get-shit-done, ClawTeam, ralph-orchestrator, ralphy) and the new Claude /goal docs + 2 prompt examples (goals-short.txt, goals-long.txt). Surface high-impact features candidates for clawpm: task definition, subtask/worktree dispatch, mission decomposition, agent orchestration patterns. Output: clawpm research entry with ranked candidates + rationale + adoption sketch.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

