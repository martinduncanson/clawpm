# Spec + context: CLAUDE.md, handoff and hook changes from the 2026-10-13 clawpm ship-loop reflect

For: the agent working on `~/.claude` core files and the handoff skill. Author: orchestrator session 5399e315 (clawpm ship-loop). Nothing here has been applied. I edited `~/.claude/CLAUDE.md` briefly and reverted it byte-for-byte (`git diff` vs HEAD is empty); no core file, skill or hook was changed by this session. Martin approved the items below in principle and chose to have you implement them. Verify each claim against the source before editing: hook and skill file names below are inferred from nudge text and are UNVERIFIED.

## Context (what happened)
A long autonomous run merged three clawpm PRs (#103 playbook, #104 `sessions write-root`, #105 `pr verify`) through a Codex review gate, resumed from a handoff baton, and went to 147% of the 200k context budget (293k) before handing off. Evidence for every item is in the session transcript: `C:/Users/Martin Workspace/.claude/projects/F--Git-clawpm/5399e315-6855-4610-9363-bca06f8670b8.jsonl`, and the final baton `~/.claude/workstreams/F--Git-clawpm/manifest.md`.

## Change 1: CLAUDE.md, merge cadence (one sentence)
Anchor: the `**Merge cadence ...**` paragraph in "Autonomy ladder". After "Review not clean/done → don't merge." insert:
> A review finding declined as false still needs an independent CLEAN re-gate before auto-merge (the classifier blocks `gh pr merge` while the latest verdict is FINDINGS).

Why: on #105 I declined a Codex P2 as a false positive (proved with a live API call). The classifier then denied `gh pr merge 105 --squash --match-head-commit <sha>` with reason "Merge Without Review". It allowed the identical command after a second gate run came back CLEAN. A declined finding is not a clean review as far as the classifier is concerned. Acceptance: the sentence exists, the merge-cadence row still reads as one paragraph.

## Change 2: CLAUDE.md, round-scoped dispatch (one sentence)
Anchor: the `**Round-scoped dispatch:**` paragraph in Delegation, ending "Workers report and stop at about 150k context." Append:
> Once the context watermark reports over 100% and a baton exists, dispatch no new rounds: tell Martin to `/clear`, and let the fresh session collect in-flight workers.

Why: after the 88% baton I kept dispatching and gating for about 20 more turns (to 147%). Each turn then costs more for the same work. Caveat: the paragraph's last line is followed by the line `<!-- delegation-rules:end -->`. Keep that marker on its own line (CRLF file); the first attempt at this edit corrupted the line break, so edit with care and confirm `git diff` shows only the added sentence.

## Change 3: handoff skill, resume must take ownership explicitly
Files: handoff `SKILL.md` §4a step 2 (binding) and `references/writer.md` §2a "Ownership check". Problem: resume binds the chosen workstream with `ledger.js use <slug> --session <me>`, but the manifest's `session:` header still names the previous session. §2a then reads "names a different session for the same default slug" as a mismatch, which says to bind a session-scoped slug and never write the mismatched file. Following that literally would fork the workstream the resume just selected. I treated the `ledger use` binding as an ownership transfer and said so.
Spec: in §2a, an ownership mismatch is NOT raised when the session's current ledger binding (from `ledger.js use`, as set by resume) names the same slug as the manifest's `workstream:` header; the writer then sets `session:` to its own id as §2a already requires. Add one line to SKILL.md §4a: "binding the workstream at resume transfers write ownership to this session". Acceptance: resume then checkpoint in a new session writes the original workstream manifest with no session-scoped fork, and a genuine concurrent writer (different binding) still triggers the fork.

## Change 4: Stop-hook ledger nudge vs the auto-mode classifier
Observed: the Stop hook `[ledger]` asks for `node ~/.claude/scripts/ledger.js add --type decision|done "..."`. The classifier denied that call ("Instruction Poisoning") when the entry text recorded an authorization claim ("Martin authorised ..."). The nudge and the permission model disagree. Options for you to pick (Martin decides which): (a) change the nudge text to say authorizations and decisions that grant autonomy go in the manifest and project memory, and `ledger add` is for neutral facts such as "PR #n merged <sha>"; (b) add a `permissions.allow` rule for `ledger.js add` (settings.json is Martin's to apply; rule syntax for this build is unverified, test it); (c) both. Acceptance: following the nudge never produces a classifier denial for a neutral done entry.

## Change 5: nudge fatigue after a baton exists
Observed over about 25 turns after a finalised baton: `[context-watermark] OVERRUN` repeated every quarter-window telling me to run baton mode again; `[periodic-save]` asked for /save 45 minutes after a baton; `[memory-discipline]` fired on every turn with file edits. Spec: when the current session's manifest is already `status: finalised` and no edits to it have happened since, the watermark nudge should collapse to one line ("baton exists at <path>; run /clear and /session-tools:handoff resume") and periodic-save should stay silent. Acceptance: with a finalised baton and no new work, no nudge asks to write another baton or checkpoint. Out of scope: lowering the thresholds.

## Observed but not mine to fix
- `~/.claude` is multi-writer and `.git/index.lock` was held by a co-session for over a minute during my attempted commit; I did not touch the lock and did not commit. The "worktree for multi-commit config edits" rule in CLAUDE.md applies to you.
- The handoff `close` step reports `"archive":"closed-existing"` or `"synthesised"` inconsistently; harmless, noted only.

## Already done by this session (no action)
Project memory only: `reference_pytest_k_filter_deselects_named_file.md` (new), one reinforcement entry in `codex_review_trajectory.md`, three classifier observations in `feedback_auto_merge_clear_prs_clawpm.md`, and its `MEMORY.md` index line (all under `~/.claude/projects/F--Git-clawpm/memory/`, untracked in the config repo).
