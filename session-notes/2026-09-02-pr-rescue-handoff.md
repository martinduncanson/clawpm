# Session manifest — F:/Git/clawpm
status: finalised  session: current  updated: 2026-09-02T~11:55Z (context 101%, finalised manually — `handoff` skill confirmed UNREACHABLE this session, not just missing from the listing: `Skill({skill:"handoff"})` returned "Unknown skill" outright. Stronger than the listing-budget theory recorded below — worth a follow-up correction to that memory next session.)

## Goal
Resumed from the 2026-09-02 08:05Z handoff. Root-caused why: `handoff`
(session-tools@claude-tools) IS installed+enabled, just absent from this
session's skill listing (heavy plugin load hitting the listing budget,
per [[reference-skill-listing-budget]] in the ~/.claude project memory —
full write-up there, feedback queued). Updated this manifest directly
instead. Triaged every stale/uncommitted worktree the prior session
flagged, rescued and finished three in-flight PR review rounds. Live
thread — NOT complete, several PRs still awaiting bot re-review.

## State of play
- **Stash `clawp069-pm-churn`** — CONFIRMED gone. `git stash list` empty.
- **Worktree `agent-a47ebd29c71d330bf` (CLAWP-098, PR #55)** — rescued.
  Held two genuine uncommitted fixes from before its dispatched session hit
  a rate limit: a round-5 grok finding (`find_session_for_cwd` OSError
  logging) that was never committed, plus I fixed a fresh round-6 Codex P1
  (`_replay`'s `Path.exists()` swallowing OSError on the session registry
  stat). Committed as 2 commits (858ef00), full suite 1530 passed, pushed,
  replied to Codex. **Awaiting round-7 re-review.**
- **Worktree `agent-a6c4d9921995e5548` (CLAWP-091, PR #56)** — same failure
  class, previously unflagged: 4 files uncommitted since ~10:44 local, no
  scratch files this time (agent died mid-edit, not mid-reply). Diff was a
  complete, well-documented fix for a live Codex P1 (falsy-non-mapping
  frontmatter coercion via `_none_to_empty`). Committed (edc1237), full
  suite 1532 passed, pushed, replied to Codex. **Awaiting round re-review.**
- **Worktree `agent-a22ec1bd29f410d6a` (CLAWP-096, PR #57)** — working tree
  was clean (no rescue needed) but had ONE fresh, unanswered Codex P1
  (final prefix-collision fallback could still return an already-claimed
  prefix when siblings use explicit `task_prefix` values). No evidence the
  dispatched session was still alive (no lock/process signal, finding sat
  ~3.5h unanswered) — picked it up directly: fixed `assign_task_prefix`'s
  last-resort arm to verify + numeric-suffix-disambiguate, added a
  regression test reproducing the exact two-sibling scenario. Committed
  (8178db3), full suite 1512 passed, pushed, replied to Codex. **Awaiting
  round re-review.**
- **`clawp068-review` worktree (CLAWP-068, PR #54)** — untouched, working
  tree clean. Last Codex pass (06:11Z) said "no major issues" — likely
  closest to mergeable of the five open PRs. Not independently re-verified
  this session.
- **PR #58 (CLAWP-103/104)** — NEW since the last checkpoint: the Fable
  planning session the prior checkpoint said "not yet started" has since
  run and opened this PR (docs/ADR/calibration-metrics-spec + emitted plan
  trees), currently shows `mergeable: MERGEABLE`. Not reviewed by me this
  session — operator/Fable-session output, needs its own read before
  treating as done.
- Five open PRs total on clawpm right now: #54 #55 #56 #57 #58.
- `~/.claude` shared config checkout still dirty/stale (session/harness-
  render-pipeline-20260814 branch, 7 commits behind origin/main) — flagged
  by SessionStart hook again this session, still not addressed; not in
  scope for this thread.

## Decisions
- CORRECTION to the prior finalised manifest's header note ("no handoff
  skill registered on disk"): that was wrong. `handoff` IS installed and
  enabled (`installed_plugins.json` + settings.json both confirm); it's
  just missing from THIS session's skill listing under plugin-count/budget
  pressure — see [[reference-skill-listing-budget]] (~/.claude project
  memory) for the full mechanism. Resumed via direct manifest read anyway
  since the skill didn't fire regardless of root cause. No action needed on
  the skill/routing-table side — this is a listing-budget bug, feedback
  queued, not a missing artefact.
- Treated "worktree has uncommitted work, dispatched session's fate
  unknown" as licence to inspect-and-rescue rather than wait, per the
  established pattern from the CLAWP-098 case earlier this thread (verify
  diff is complete/coherent, run full suite, only then commit+push) — did
  NOT just discard or leave any of the three worktrees untouched.

## Key files
- PRs: #54, #55, #56, #57, #58 (all `martinduncanson/clawpm`).
- Worktrees touched: `.claude/worktrees/agent-a47ebd29c71d330bf`,
  `.claude/worktrees/agent-a6c4d9921995e5548`,
  `.claude/worktrees/agent-a22ec1bd29f410d6a` — none removed yet, all still
  have an open PR awaiting bot re-review.
- `src/clawpm/sessions.py`, `src/clawpm/frontmatter.py`,
  `src/clawpm/research.py`, `src/clawpm/tasks.py` — files touched this
  session (across the three worktrees above).

## Nuances
- The CLAWP-091 worktree rescue had NO leftover scratch/reply files (unlike
  CLAWP-098's), suggesting that dispatched agent died mid-EDIT rather than
  mid-reply — a different stall signature worth knowing if this pattern
  recurs (check for `git diff`, don't assume "no scratch files" means "no
  work to rescue").
- The CLAWP-096 P1 fix required reasoning about `_portfolio_prefixes` /
  `assign_task_prefix` semantics (explicit vs. derived prefixes) rather
  than being a small mechanical patch — worth a closer look at whether
  Codex flags anything else in that same collision-detection area on
  re-review.
- Main checkout's `.project/tasks/CLAWP-091/096/098.{md→progress.md}`
  in-flight dispatch markers remain deliberately uncommitted (per the prior
  checkpoint) — do not mark these tasks done until their PRs actually
  merge; the code fixes landing today are review-response commits, not
  task completion.

## Next action (fresh session start here)
1. All 5 open PRs (#54/#55/#56/#57/#58) show `mergeable: MERGEABLE` as of
   11:36Z (git-conflict-free ONLY — per [[feedback-pr-mergeable-not-review-approved]]
   in ~/.claude project memory, this is NOT proof of review-clean). #56 and
   #57 both show `updatedAt` a few minutes AFTER my last reply on each
   (11:28Z, 11:36Z) — something new landed (likely CI finishing, possibly a
   fresh bot comment). **First move: re-check comments on #55/#56/#57 for
   anything posted after this manifest's `updated` timestamp** before
   assuming they're clean.
2. Once a PR shows a clean bot pass + green CI, it's operator-owned/low-
   blast-radius per git-discipline — auto-mergeable (L2) unless a later
   round changes that assessment.
3. Read PR #58 (CLAWP-103/104 Fable output) before touching it — new,
   unreviewed by any session so far.
4. `clawp068-review` (PR #54) — last Codex pass (06:11Z) was clean; worth
   confirming it's actually mergeable now.
5. **Skill-loading bug is WORSE than first recorded**: `Skill({skill:"handoff"})`
   returned `Unknown skill` directly (not a listing/display issue) —
   go correct [[reference-skill-listing-budget]] in the ~/.claude project
   memory (currently says "absent from listing," should say "unreachable
   via Skill tool entirely") before writing anything else there. The queued
   SendFeedback draft from this session covers the listing-absence half;
   consider whether it needs amending too.
6. `~/.claude` shared config checkout still dirty/stale (session/harness-
   render-pipeline-20260814, 7 commits behind origin/main) — untouched,
   out of scope for this thread, flagged again by this session's start hook.
