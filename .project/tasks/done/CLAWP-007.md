---
complexity: m
created: '2026-05-15'
depends:
- CLAWP-006
id: CLAWP-007
predictions:
  approach: 'Phase A: define minimum-viable JSON schema (purpose, entry_points, top_level_modules,
    load_bearing_invariants, external_dependencies, last_generated_commit). Phase
    B: write a Gemini-Flash-based generator that takes the repo as input, returns
    JSON conforming to the schema. Phase C: GitHub Action regenerates on commit-to-main
    and opens a PR (NOT auto-commit — drift is the killer per the design discussion).
    Phase D: integrate as prepended context for PR-Agent (workflow env var carries
    the JSON or a digest). Phase E: alternative path to evaluate — embed the same
    content into CLAUDE.md and test if that''s strictly better than a separate file.
    Phase F: 7-PR A/B comparison: same diff reviewed with vs without the map prepended;
    compare findings.'
  complexity: m
  confidence: 2
  duration_min: 180
  files_changed: 3
  files_scope:
  - .project/architecture.json
  - .github/workflows/regenerate-architecture.yml
  - skills/clawpm/SKILL.md
  filled_by: agent
  hypothesis: Prepending architectural map to PR-Agent's review context catches at
    least one finding/PR that the map-less review misses, across non-trivial diffs.
    If yes, roll out to other clawpm-tracked projects. If no, kill the experiment
    (or fold the JSON into CLAUDE.md as a passive doc artifact).
  pre_mortem: 'Most likely failure: the map is too coarse or too noisy to actually
    move PR-Agent''s review needle. Mitigation: A/B test before committing to portfolio
    rollout. Secondary failure: schema drift across regenerations — the LLM produces
    structurally-different JSON each run. Mitigation: enforce schema validation in
    the generator; reject + retry on schema violation.'
  reference_tasks:
  - CLAWP-005
  success_criteria:
  - Schema documented and stable across at least 5 regenerations
  - Generator runs in <60s on the clawpm repo via Gemini Flash free tier
  - GitHub Action opens a PR with the regenerated map on each main-commit; doesn't
    auto-merge
  - A/B test on 7 PRs shows map-prepended reviews catch ≥1 finding/PR that map-less
    reviews miss (or proves they don't)
  - 'CLAUDE.md alternative path evaluated: are we strictly better with a separate
    JSON or with this content merged into CLAUDE.md?'
  unknowns: (1) Whether Gemini Flash can produce stable structured output across runs
    without drift — needs response_schema constraint. (2) Whether the JSON is the
    right surface or whether all of this is just CLAUDE.md content. (3) Whether bot
    reviewers (PR-Agent, Codex) can actually consume prepended context via workflow
    env vars OR whether we need to inject it into the PR description. (4) Whether
    gitnexus integration (CLAWP-006 candidate) would moot this work by giving richer
    context for free.
priority: 5
---
# Architectural map artifact (gated on CLAWP-006): prototype .project/architecture.json on clawpm; test if prepending it to PR-Agent briefs improves review quality



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

