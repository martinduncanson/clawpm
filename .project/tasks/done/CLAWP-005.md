---
complexity: m
created: '2026-05-15'
id: CLAWP-005
predictions:
  approach: '1) install-gate vet pr-agent install (pip/docker/Action — pick by gate
    output). 2) Install + configure Gemini Flash backend with GEMINI_API_KEY. 3) Smoke-test
    on clawpm#3 (already-merged, public, low-risk). 4) Compare review quality to Codex''s
    clean review on same PR. 5) On a feature branch in clawpm: swap doctrine in clawpm
    SKILL.md workflow grid + memory file (CodeRabbit → PR-Agent + Gemini Flash). Move
    CodeRabbit to footnote. 6) Merge to main, push fork, sync clones. 7) Set evaluation
    reflection checkpoint for 2026-05-18 (3 days).'
  complexity: m
  confidence: 3
  duration_min: 90
  files_changed: 4
  files_scope:
  - skills/clawpm/SKILL.md
  - memory/feedback-reviewer-triangle-is-deliberate-redundancy.md
  filled_by: agent
  hypothesis: Owning the reviewer harness (PR-Agent + free-tier Gemini Flash) eliminates
    the CodeRabbit paywall for private repos AND gives us prompt-level control. Quality
    matches or beats CodeRabbit for the post-commit gate; if not, swap back is trivial
    since doctrine is one file.
  pre_mortem: 'Most likely failure: PR-Agent install fails or Gemini Flash backend
    mis-config produces empty reviews. Mitigation: smoke-test BEFORE the doctrine
    swap. If smoke-test fails, try Qwopus (local) backend OR delay doctrine swap and
    file an issues entry. Don''t swap doctrine until at least one substantive review
    lands.'
  reference_tasks:
  - CLAWP-002
  success_criteria:
  - PR-Agent installed without polluting global Python env (pipx or Docker or uv tool)
  - Smoke-test review on clawpm#3 returns substantive output (not just summary)
  - Doctrine swap lands on clawpm main; runtime clones synced
  - Evaluation checkpoint set in clawpm for 2026-05-18
  - CodeRabbit references retained as footnote/alternative, not deleted (so swap-back
    is one revert)
  unknowns: (1) PR-Agent's exact install path on Windows (pipx vs uv tool vs Docker).
    (2) Whether GEMINI_API_KEY is already set in env. (3) Whether PR-Agent's structured-output
    handling on Gemini Flash matches what we saw on Codex (Codex's structured output
    is excellent; Flash's may need response_schema constraint). (4) Quality benchmark
    — what's 'substantive enough' for the smoke-test to pass?
priority: 5
---
# Swap CodeRabbit → PR-Agent + Gemini Flash in reviewer-triangle doctrine; ship to main; evaluate in 3-5 days



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

