---
created: '2026-07-06'
id: clawpm-research-prism-repo-research-linked-issue-context-injection
status: open
type: investigation
---
# PRism repo-research: linked-issue context injection + auto-labeling, applicability to clawpm

## Question

Sidebar analysis of github.com/SkySingh04/PRism (self-hosted AI PR review tool) — assess linked-issue context injection and content-based auto-labeling for code-quorum, and classifier/embedder/reranker fit within clawpm specifically

## Summary

PRism (github.com/SkySingh04/PRism, 9 stars, TS, MIT) is a self-hosted AI PR-review bot: customizable RULES.md/YAML rule sets, static analysis + LLM review, swappable LLM backend (incl. Ollama/local), GitHub PR integration with inline comments. More directly relevant to code-quorum (the operator's own multi-model review tooling) than to clawpm's core, but the operator specifically asked whether classifiers/embedders/rerankers have a place IN clawpm.

## Findings

- **2.1 Linked-issue context injection** (regex the PR title/body for `#123` refs, pull each issue's full thread via GitHub API, thread into the review prompt) — near-zero-cost, gives reviewers the "why" not just the diff. code-quorum doesn't do this today. Candidate for a small code-quorum follow-up spec; not currently in its backlog. Not a clawpm concern (clawpm doesn't run PR reviews).
- **2.2 Auto-labeling from review content** (cheap classifier over the LLM's analysis, tags the PR by risk area) — PRism itself frames this as a minor triage nicety. Same verdict applies wherever it'd be adopted.
- **Classifier/embedder/reranker fit within clawpm specifically:** clawpm's `reflect.py` reference-task similarity scoring (`_similarity_score`) is ALREADY explicitly designed as an embedding-swap point — its own docstring states: "The matching is intentionally simple — no embeddings, no LLM... Phase 2 can swap in something smarter; the API surface stays the same." So this isn't a new idea, it's a deferred one the codebase already anticipated.
- A second, concrete, campaign-motivated candidate: near-duplicate task detection at `tasks add` time. This session hit exactly this failure mode twice (CLAWP-089/090 filed as accidental duplicates during a cross-worktree ID race) — a cheap local-embedding similarity check against open/recent tasks at add-time ("this looks 90% similar to CLAWP-XXX, confirm?") would catch it deterministically without an LLM call.
- Auto-tagging (mirroring PRism's 2.2) is the weakest fit for clawpm too — same "minor, low priority" verdict, now that CLAWP-069 tags exist.
- Hard constraint: clawpm's core emission layer is a **test-enforced zero-LLM-calls guarantee** (the judgment/facts seam is load-bearing architecture, not incidental). Any classifier/embedder/reranker must live as an OPTIONAL, swappable judgment-layer add-on (same posture as the existing CodeGraph-symbol axis in `_similarity_score`, which is opt-in and no-ops when absent) — never a required dependency of the deterministic core.

## Conclusion

Two candidates worth a real look, both already have a designed seam to slot into: (1) swap `reflect.py`'s reference-task similarity for a real (local, no-cloud) embedding model — the docstring already invites it; (2) add a cheap embedding-based near-duplicate check at `tasks add` time, directly motivated by this campaign's own CLAWP-089/090 collision. Auto-tagging/auto-labeling: skip, low value for clawpm's shape. Not filed as a task yet — worth a small spike/decision task if the operator wants to pursue either.
