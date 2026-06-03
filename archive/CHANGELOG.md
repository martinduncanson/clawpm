# Archive changelog

Reversible record of repo-hygiene moves. Nothing here is deleted — archived
files retain full git history (moves were `git mv`, tracked as renames).

## 2026-06-03 — Repo hygiene: archive stale root docs + relocate spec

**Action:** Triaged the loose top-level docs (repo-hygiene skill, adapted for a
code repo — classification by content + git-recency rather than transcript
mining). Moves:

- **Archived → `archive/stale/`:**
  - `agent_smoke_test.md` — untouched 4 months.
  - `PR-PLAN.md` — one-shot "upstream PR-chunking plan", target exec 2026-05-22 (passed).
  - `UPSTREAM-BRIEF.md` — fork→upstream snapshot ("41 commits ahead", drafted 2026-05-22); now outdated. Regenerate fresh for the next upstream PR.
- **Relocated:** `WORKFLOW-RUNTIME-INTEGRATION.md` → `docs/` (a proposed spec, not root material). Cross-refs updated in `docs/playbooks/dispatch-fan-out.md`, the spec itself, and the `UPSTREAM-BRIEF.md` pointer dropped from `AGENTS.md`.

**Rationale:** The top level was accreting operator-meta / dated planning docs
that drown the load-bearing root files (README, AGENTS, CLAUDE, pyproject).

**Kept on fork, fork-internal (exclude from any upstream PR via an `upstream-sync`
cleanup branch, NOT gitignore):** `ROADMAP.md` (refresh pending — stale re
CLAWP-037..041), `sync-runtime-clones.sh` (machine-specific), `.gemini/` (reviewer
config), `docs/WORKFLOW-RUNTIME-INTEGRATION.md`.

**Review date:** 2026-07-03 (+30d) — re-evaluate whether the archived docs can be deleted.
**Status:** applied.
