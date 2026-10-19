# Project-health audit pipeline (CLAWP-099)

**Decision: the audit is a clawpm-planner MODE (`audit`), not a separate skill.** It reuses the planner's constitution, vet and emit stages unchanged, and adds one new stage in front of them: a parallel fan-out of analysis-dimension agents. The deterministic part (validate, dedup, cap, map to emit-tree) is one core module, `src/clawpm/audit_findings.py`. Round 1 ships the decision, that module and its tests. The SKILL.md mode prose is round 2.

## Alternatives

- **Separate `clawpm-audit` skill.** Rejected. It would copy the constitution load, the won't-do diff, the emit contract and the idempotent re-emit rules. The task pitfall names this exactly: a parallel system with its own maintenance burden, run rarely, drifting from the planner.
- **New CLI command that runs the agents.** Rejected. Core makes zero LLM calls (planner seam: the skill decides, core persists). Fan-out is harness work (the Agent tool), so it belongs in skill prose.
- **Mode (chosen).** The planner already has modes by rule (chart mode, hard rule 7). Audit follows the same shape: a trigger, a stage subset, an exit.

## What it reuses

Constitution load (stage 1), the vet stage (re-read cited ground, reject by-design and duplicates, diff against the won't-do ledger), `clawpm tasks emit-tree` with its gates (schema, reject-match, constitution, id collision, baseline), `leaf_key` idempotent re-emit via `attach_to`. Nothing in `emit_tree.py` changes.

## Pipeline

1. **constitution** (existing).
2. **fan-out** (new): one agent per dimension, in parallel, read-only. Each returns a JSON list of findings (schema below). An empty list is a valid answer.
3. **normalise + dedup + cap** (new, code): `audit_findings.parse_findings` then `build_emit_document`.
4. **vet** (existing, model): re-read the cited file:line for each surviving finding, drop by-design behaviour and won't-do matches.
5. **emit** (existing): one `emit-tree --dry-run`, then the real call, then PAUSE (hard rule 5).

## Dimensions

`correctness`, `security`, `consistency`, `tests`, `dead_code`, `tooling`, `agentic_setup` (the Turbo `/audit` lenses plus clawpm's own agent-facing surface: skills, hooks, CLAUDE.md, MCP).

## Finding schema

```json
{"dimension": "security", "file": "src/clawpm/serve.py", "line": 5,
 "title": "Token logged in plaintext", "severity": "critical",
 "evidence": "serve.py:5 logger.info(token)"}
```

`severity` is `critical|high|medium|low|info`. `line` is a positive integer or null. Every other field is a non-empty string. Unknown keys, a wrong dimension group, or a bad value raise `AuditFindingError` listing every problem. Nothing is skipped silently (same fail-closed stance as `parse_emit_document`).

## Dedup key

Within one normalised path (backslashes to `/`, leading `./` stripped), title token sets (lowercase alphanumerics, stopwords dropped) with Jaccard >= 0.6 merge, on the same line or a different one. A shared line alone never merges: unrelated defects (a locking bug and a token-logging bug) can sit on one line, and merging them would hide one with no dropped marker.

A title with no tokens at all (single characters such as `X`, or non-Latin scripts) cannot use Jaccard. Two such titles merge only when their normalised full titles (casefolded, whitespace collapsed) are equal; an empty-token title never merges with a token-bearing one.

The first match wins, over input sorted by every field (file, line, dimension, title, severity, evidence), so the result, including which evidence survives the 300-char truncation, does not depend on agent return order. A merged finding keeps the highest severity, the union of dimensions, and all distinct evidence. Corroboration by several dimensions is signal and sorts earlier. The fuzzy stage is deliberately conservative: a missed merge costs one duplicate leaf that vet catches; a false merge hides a defect.

## emit-tree mapping

One new root (`root.title`), flat leaves ordered by severity, then corroboration count, then location. Per leaf:

| emit-tree field | Source |
|---|---|
| `ref` | `f001`... in sorted order |
| `title` | finding title, unprefixed so the exact-title won't-do match can fire |
| `leaf_key` | `audit-` + sha1 prefix of file, line and sorted title tokens (the normalised full title when there are no tokens); stable across runs |
| `success_criteria` | one WHEN/THEN criterion; `gradeable_signal` is the evidence (truncated to 300 chars) |
| `scope` | `[file]` |
| `delegability` | `either` |

Re-running the audit against the same root with `attach_to` skips leaves whose `leaf_key` already exists. A moved line changes the key, so a finding that shifts may re-emit once; vet and the title-based won't-do gate are the backstop.

## Volume control

`max_findings` (default 40) keeps the top of the sorted list. The report names every dropped finding (`dropped_findings`), so the cap is visible and the skill must show it to the operator. Run low severities again with a narrower scope rather than raising the cap.

## Failure modes

- **Agent returns malformed JSON or a bad field:** whole batch rejected with every problem listed; the skill re-asks that one dimension, never hand-patches.
- **Dimension agent times out or returns nothing:** indistinguishable from "no findings" in the data, so the skill must state which dimensions ran. Round 2 prose requires that line in the audit summary.
- **Audit finds nothing:** a successful no-op. `build_emit_document` returns `doc = None` and `report["nothing_to_emit"] = true`; the skill reports "0 findings, nothing to emit" and does not call emit-tree, whose parser rejects an empty `leaves` list.
- **Two agents describe one defect in unrelated words:** survives as two leaves; vet catches it.
- **Cap drops a real finding:** visible in the report, not silent.
- **Audit run rarely, rots:** mitigated by there being almost no new surface (one module, one skill section).
- **Findings from a dimension agent are untrusted input:** they are model output over repo content. Vet re-reads the cited ground before emit.

## Out of scope for round 1

SKILL.md mode prose, dimension-agent prompts, a CLI wrapper around `build_emit_document`, a durable report artifact (research entry). Round 2 covers the prose and prompts.
