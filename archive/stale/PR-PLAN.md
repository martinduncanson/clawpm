# Upstream PR-chunking plan — malphas-gh/clawpm

**Target execution:** 2026-05-22 22:00 EEST (19:00 UTC). Operator drives `gh` commands; this plan is the input.

**Fork ahead:** 46 commits, +8,900 LOC, 35 files (after this session's commits). Branch: `feat/doctor-encoding-check-clawp-011` on `martinduncanson/clawpm`. Need to flow into `main` first, then chunk upstream.

## Pre-flight (do this before any PR)

```bash
cd F:/Git/clawpm
git fetch origin

# 1. Merge feat/doctor-encoding-check-clawp-011 into local main first
git checkout main
git merge --ff-only feat/doctor-encoding-check-clawp-011
git push fork main   # sync fork's main with feature branch's state

# 2. Confirm upstream hasn't moved since last fetch
git log origin/main..main --oneline | wc -l   # should be 46
git log main..origin/main --oneline           # should be empty (clean ahead-only)

# 3. Test sweep
python -m pytest -q   # expect 376/376
```

If upstream `origin/main` has advanced, **stop and reassess** — the chunking below assumes a stationary upstream.

## Chunking strategy

12 thematic PRs grouped into 4 waves. Land waves sequentially so reviewers see coherent stories; PRs within a wave can be reviewed in parallel.

**Wave priority**: portability fixes first (cheapest to merge, no API surface). Then features. Then opinionated/agent-runtime stuff last (gives reviewers warm-up before the bigger asks).

---

### WAVE 1 — Foundational fixes (target: review-ready immediately)

#### PR 1 — Windows TOML + cp1252 portability
**Cherry-picks:** `8be39bc cf0ade2 ac65023 e99b400 c467069 087eb3a 94c54f3`
**Universal:** HIGH · **Risk:** LOW · **Depends on:** none
**Pitch:** Windows-default codepage and TOML backslash silent-swallow cost real debug sessions. Pure bug fixes; no API change.
**Files:** `src/clawpm/cli.py`, `src/clawpm/discovery.py`, `src/clawpm/models.py`, `src/clawpm/output.py`, `tests/test_subtasks.py`, `tests/test_encoding_and_dedup.py`, `tests/test_bug_fixes.py`

#### PR 2 — `scope` field + `clawpm conflicts` command
**Cherry-picks:** `9c8a228 5199479 a54ffa6`
**Universal:** HIGH · **Risk:** LOW · **Depends on:** none
**Pitch:** Pre-flight collision detection for parallel-agent dispatch. Additive: existing tasks without `scope` continue working.
**Files:** `src/clawpm/cli.py`, `src/clawpm/models.py`, `src/clawpm/tasks.py`, `tests/test_scope.py`

#### PR 3 — Issues observation type + `--tag` filter
**Cherry-picks:** `604998b`
**Universal:** HIGH · **Risk:** LOW · **Depends on:** none
**Pitch:** Distinguish "must-fix" from "worth-noting" in the issue stream. Adds an enum value and a filter; backwards-compatible.
**Files:** `src/clawpm/cli.py`, `src/clawpm/models.py`, `tests/test_issues_observation_and_tags.py`

---

### WAVE 2 — Reflect subsystem (target: review-ready ~T+30min after WAVE 1 acks)

#### PR 4 — Phase 1 reflect: predictions, actuals, deltas
**Cherry-picks:** `8af08dc 5631fdf 97d3903`
**Universal:** MEDIUM-HIGH · **Risk:** MEDIUM · **Depends on:** PR 1 (test fixtures)
**Pitch:** Turn the PM layer into a calibration substrate. Adds prediction flags on task add, computes actuals on done, emits deltas to `~/clawpm/reflections/<id>.jsonl`. Six v1 fixes already integrated.
**Files:** `src/clawpm/reflect.py` (new), `src/clawpm/tasks.py`, `src/clawpm/cli.py`, `tests/test_reflect_phase1.py`, `tests/test_reflection_uptake.py`
**Notes for upstream:** May want to gate behind `[reflect] enabled = true` in portfolio.toml — opt-in vs default-on is a real question.

#### PR 5 — Phase 1.5 applied-science predictions + recursive meta-reflection
**Cherry-picks:** `a0ce847 ca335a5`
**Universal:** MEDIUM · **Risk:** MEDIUM · **Depends on:** PR 4
**Pitch:** Adds `--success-criteria`, `--hypothesis`, `--pre-mortem`, `--confidence`, `--unknowns`, `--reference-task`, `--predicted-by`, `--process-lesson`, `--surprise` (fixed taxonomy). Opinionated — upstream may want flags toggleable.
**Files:** `src/clawpm/reflect.py`, `src/clawpm/cli.py`, `src/clawpm/models.py`, `tests/test_reflection_phase1_5.py`

#### PR 6 — Phase 1.6 doctor checks + reflect void + filled_by attribution
**Cherry-picks:** `3619c50 7d96210`
**Universal:** HIGH (doctor checks) / MEDIUM (filled_by) · **Risk:** LOW · **Depends on:** PR 4
**Pitch:** New doctor checks (stale tasks, drift, prefix collisions). `reflect void` marks bad reflections. `Predictions.filled_by` tracks agent-vs-operator attribution.
**Files:** `src/clawpm/cli.py`, `src/clawpm/reflect.py`, `tests/test_phase16.py`

---

### WAVE 3 — Standalone features (can run in parallel)

#### PR 7 — Phase 1.7 inbox (inter-agent messaging)
**Cherry-picks:** `434739d 3f9b5e3`
**Universal:** MEDIUM · **Risk:** MEDIUM · **Depends on:** none
**Pitch:** Persistent message queue for cross-agent / cross-session handoff. Opinionated about multi-agent workflows.
**Files:** `src/clawpm/inbox.py` (new), `src/clawpm/cli.py`, `tests/test_inbox.py`

#### PR 8 — Phase 1.8 project announce + commit-drift + missing-marker
**Cherry-picks:** `c884486 fef403e 71a3451 844180a` (latter two are codex-review fixes — verify they apply cleanly)
**Universal:** MEDIUM · **Risk:** LOW · **Depends on:** PR 6 (doctor framework)
**Pitch:** `clawpm projects announce` writes a clawpm-tracked marker into CLAUDE.md/AGENTS.md/README.md. Doctor flags missing markers and `work_log` drift vs git commits.
**Files:** `src/clawpm/announce.py` (new), `src/clawpm/cli.py`, `tests/test_phase18_drift_and_announce.py`

#### PR 9 — Doctor `--check-codex` (CLAWP-008)
**Cherry-picks:** `3173e4e 832c1f5 1cbca9a`
**Universal:** MEDIUM · **Risk:** LOW · **Depends on:** PR 6 (doctor framework)
**Pitch:** Walks recent PRs per project for `chatgpt-codex-connector[bot]` activity; warns when missing. Off by default.
**Files:** `src/clawpm/codex_check.py` (new), `src/clawpm/cli.py`, `tests/test_codex_check.py`

#### PR 10 — Doctor `--check-encoding` (CLAWP-011)
**Cherry-picks:** `bfdf758 18359f1`
**Universal:** HIGH · **Risk:** LOW · **Depends on:** PR 6 (doctor framework) and ideally PR 1 (operator empathy on the bug class)
**Pitch:** AST-scans `.py` files for the three cp1252-risk patterns (non-ASCII in print/echo, missing encoding= kwarg, missing stdout.reconfigure). Off by default.
**Files:** `src/clawpm/encoding_check.py` (new), `src/clawpm/cli.py`, `tests/test_encoding_check.py`

---

### WAVE 4 — Agent-runtime adapters + restorations (most opinionated; land last)

#### PR 11 — AGENTS.md template + codex-instructions docs
**Cherry-picks:** `41b4725`
**Universal:** HIGH · **Risk:** LOW · **Depends on:** none
**Pitch:** Drop-in template for Codex / agents-spec runtimes to use clawpm. No code change.
**Files:** `AGENTS.md.template`, `codex-instructions.md`

#### PR 12 — Restorations: examples/portfolio + history-import + clawpm-sync hook
**Cherry-picks:** `ca69dc4`
**Universal:** MIXED · **Risk:** MEDIUM (re-introduces sessions-extractor lineage) · **Depends on:** PR 4 (reflect framework)
**Pitch:** Restores three features upstream removed:
- `examples/portfolio/` — onboarding fixtures (pure docs).
- `clawpm reflect history-import` — VT-clean redesign of the deleted `sessions.py`. NO hardcoded paths (env var + flag); generic JSONL scanner; lazy-imported. Regression-guard tests assert the discipline.
- `hooks/clawpm-sync/` — Claude Code hook rewrite (Python, not TS) for auto work-log capture.
**Note:** explicitly addresses upstream's `a06a5b8` removal rationale ("VirusTotal is an alarmist arse") by removing every static pattern VT flagged. PR description should link to the rationale and tests.

---

### EXPLICITLY SKIPPED (do not upstream)

| Commit | Reason |
|---|---|
| `7124c21` | Fork-specific install URL switch |
| `dc79695 47256ab` | `sync-runtime-clones.sh` — operator's runtime-mirror helper |
| `b16770a 85b57ce` | PR-Agent CI workflow — calibration data ambiguous, operator-specific |
| `0cfd73c ffb6da8 f07a9bd 8f3dec9` | Fork-specific ROADMAP |
| `38630fa` | clawpm-cowork skill bundling — depends if upstream wants it |
| `0dd5491` | temp/ gitignore — squash into PR 1 if convenient |
| `c01046a` | README cross-refs for fork-only features |
| `80354c1` | `AGENTS.md` + `UPSTREAM-BRIEF.md` at repo root (UPSTREAM-BRIEF is FOR upstream's maintainer to read, not commit) |
| `b5f81b7` | README quickstart rewrite — discuss with upstream first, may want preserved |
| `ee9a3d7 35dc02b 846d45c` | SKILL.md doc-only changes (fork's workflow grid) — bundle as one if upstream wants |

---

## Execution sequence (22:00 EEST)

For each PR in wave order, the standard recipe is:

```bash
# 1. Create branch off upstream main
git checkout -b upstream-pr-NN origin/main

# 2. Cherry-pick the commits (in chronological order)
git cherry-pick <oldest-hash> <next-hash> ... <newest-hash>
# Resolve conflicts if any; abort the wave if the conflict is non-trivial.

# 3. Confirm tests pass
python -m pytest -q

# 4. Push to fork
git push fork upstream-pr-NN

# 5. Open PR against upstream (NOT fork)
gh pr create \
  --repo malphas-gh/clawpm \
  --base main \
  --head martinduncanson:upstream-pr-NN \
  --title "<title from this plan>" \
  --body-file <(cat <<'EOF'
## Summary
<3-5 bullets — what + why>

## Compatibility notes
<additive / opt-in / breaking — and why>

## Tests
<count + key cases>

## Context (optional)
<link to fork branch, issue, or design discussion>
EOF
)

# 6. Watch for upstream feedback; address before opening the next wave's PRs.
```

**Don't open all 12 PRs at once.** Drown rate is real. Open Wave 1 (3 PRs), wait for at least one review acknowledgement (positive or negative), use the signal to refine Wave 2's framing.

**If a PR gets a "no thanks":** record the rationale in `UPSTREAM-BRIEF.md`'s pending list, don't argue, move on. The fork stands on its own.

**If the conflict resolution for any cherry-pick is non-trivial:** abort the cherry-pick (`git cherry-pick --abort`), file a clawpm issue, and either prepare a manual patch OR skip that PR for tonight.

---

## Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Upstream `main` advanced since last fetch | Medium | Pre-flight step 2 detects; if so, re-run plan with fresh log |
| Cherry-pick produces messy conflicts (e.g. file paths overlap) | Medium | Per-PR `--abort` clause; manual patch fallback |
| Upstream maintainer (malphas-gh) inactive — no review | High | Plan tonight is to OPEN PRs, not close them. Review can take days. Track in `UPSTREAM-BRIEF.md` "pending response" section. |
| Phase numbering (1.5, 1.6, 1.7, 1.8) reads as fork-specific | High | In each Wave 2-3 PR body, propose renaming if upstream prefers flat versioning (e.g. "Reflections v2" instead of "Phase 1.5"). Don't rename in the diff. |
| Some commits span multiple thematic groups | Medium | Per-PR cherry-pick rather than range merge; willing to leave commits out of any one PR. |
| Codex / PR-Agent fires on upstream PRs and adds noise | Low | Tag bots only if upstream has them connected; don't @-mention unprompted. |

---

## Timer

- **22:00 EEST:** start. Pre-flight + Wave 1 PRs (3 PRs).
- **22:30 EEST:** open Wave 1. Buffer.
- **23:00 EEST:** Wave 2 (3 PRs).
- **23:30 EEST:** Wave 3 (4 PRs in parallel).
- **00:00 EEST:** Wave 4 (2 PRs).
- **00:30 EEST:** sweep — close any session-local clawpm tasks, commit `UPSTREAM-BRIEF.md` update with "pending response" section, log session.

If the operator wants to spread across multiple evenings, Wave 1 + 2 tonight + a 24-hour pause for review signal is the better-paced version.

---

## Post-merge sweep (whenever upstream lands one)

- Pull upstream changes into fork: `git checkout main; git fetch origin; git merge --ff-only origin/main`
- Update `UPSTREAM-BRIEF.md`: move landed PRs from "open" to "landed"; capture any review notes worth keeping
- Run `clawpm doctor` against fork's `~/clawpm/projects/clawpm/` to verify no drift
