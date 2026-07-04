---
created: '2026-06-26'
id: clawpm-research-claude-task-master-agentbox-eval-for-clawpm
status: open
tags:
- sprint-scouting
- mcp-interface
- parallel-dispatch
- backend
type: investigation
---
# claude-task-master + agentbox: eval for clawpm

## Question

Value to clawpm from eyaltoledano/claude-task-master (27.7k star, JS, NOASSERTION licence) and madarco/agentbox (134 star, TS, MIT)?

## Summary

VERDICT: task-master is clawpm's CLOSEST SIBLING (AI task-mgmt for AI-driven dev) - mine for FEATURES not code (licence not clean MIT): standout idea = MCP-server-first interface (its 27.7k stars come from dropping into ANY editor - Cursor/Windsurf/VS Code/Claude Code via MCP; clawpm is Claude-Code-skill-bound) -> roadmap candidate 'clawpm MCP server' to broaden reach; also tags/workstreams (multi-context task grouping), 'loop' automation command, multi-provider model config (main/research/fallback), PRD->tasks parsing (clawpm-planner overlaps). agentbox = parallel sandboxed agent VMs (Docker FUSE overlay local + Hetzner/Daytona/Vercel/E2B cloud), checkpoints (sub-1s box startup), auto-pause, git-creds-stay-local, tmux detach, per-box browser/VSCode/VNC. Same category as crabbox (last sprint) -> another BACKEND candidate for CLAWP-065/052 cross-machine/off-harness dispatch; its checkpoint/fast-startup model is compelling for CLAWP-021 parallel_group BATCH dispatch (spin N boxes from a warm checkpoint). NET: task-master -> feature-mine (MCP interface = highest-value new idea); agentbox+crabbox -> pick ONE dispatch backend (crabbox=lease/test-runner+spend-caps, agentbox=full-box/checkpoints/parallel) - agentbox wins for parallel_group, crabbox for metered remote test runs.
