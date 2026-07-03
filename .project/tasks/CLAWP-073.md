---
baseline_ref: 0b307f2
complexity: s
created: '2026-07-03'
id: CLAWP-073
predictions:
  confidence: 4
  duration_min: 90
  filled_by: agent
  pre_mortem: 'Most likely failure: partial update leaves NEW internal contradictions;
    mitigate by grepping SKILL.md for every stale term (phase2_pending, Phase 2 stub,
    Gemini) after editing'
  success_criteria:
  - No section of SKILL.md claims reflect summarize/suggest/history-import are Phase
    2 stubs
  - Full Command Reference includes emit-tree, dispatch, agent dispatch, lease, judge
    tournament, constitution, mission, decompose, teardown-dispatch
  - Review-tooling section matches current doctrine (Codex + grok-build + grok-composer
    + Antigravity; Gemini bot retired 2026-06-24)
  - Spot-check of 10 documented commands against cli.py finds zero syntax mismatches
priority: 3
scope:
- skills/clawpm/SKILL.md
---
# Reconcile bundled SKILL.md with shipped reality

HIGH-impact docs defect (audit 2026-07-03): skills/clawpm/SKILL.md is the first thing every agent loads, and it actively misinforms. Lines 533-542 label reflect summarize/suggest as Phase 2 stubs returning phase2_pending - they shipped with CLAWP-040 (2026-05-28, cli.py:5314/5353) and the file CONTRADICTS ITSELF (its own capability map line 44 lists them as working). It also omits the entire agentic layer (emit-tree, dispatch, lease, judge tournament, constitution, mission) and still names the retired Gemini Code Assist bot as an elevated parallel primary (superseded by grok+antigravity doctrine 2026-06-30).

SPEC: (1) delete the Phase 2 stubs section; document reflect summarize/suggest/history-import/void as shipped in the reflection reference. (2) Add the agentic-layer commands to the Full Command Reference, cross-checking each against cli.py --help. (3) Update the review-tooling / workflow-integrations section to current four-surface doctrine. (4) Keep the project-level reflection stub section ONLY if project reflect is genuinely still unshipped (verify against cli.py). (5) Remember the mirror rule: ~/.claude/skills/clawpm is a git CHECKOUT auto-synced by post-merge hook - edit only the repo copy.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

