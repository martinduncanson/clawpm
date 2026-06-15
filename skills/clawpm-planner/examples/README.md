# clawpm-planner — worked demonstrations (validation evidence)

These two emit-tree documents are the **proof that the skill's emit stage targets
the real CLAWP-056 contract**. Each was produced by walking the skill's stages by
hand on a free-text objective, then validated against the live CLI in a throwaway
project (`~/clawpm/projects/planner-demo`, never the clawpm backlog).

Re-run any of them yourself:

```bash
cat <file>.json | clawpm tasks emit-tree --dry-run     # validate, no writes
cat <file>.json | clawpm tasks emit-tree               # emit for real
```

## Files

| File | Objective | Kind | Scale | Shows |
|---|---|---|---|---|
| `software-autosave.emit.json` | "Add draft autosave to the note editor" | software | m | parent + 3 vertical-slice leaves, each with rubric + scope/out_of_scope/stop/delegability + predictions, linked PRD |
| `knowledge-competitor-brief.emit.json` | "Produce a competitor-pricing analysis report" | knowledge-work | m | parent + 3 vertical-slice leaves whose `scope` are **named deliverables**, not file globs; PRD; NO code-audit assumptions; graph-absent remediation tagged in the PRD |
| `software-autosave.reemit-attach.json` | re-emit of the same software tree via `attach_to` | software | — | idempotency: matching `leaf_key` is skipped, only the new leaf emits |
| `software-multilevel.emit.json` | "Collaborative comments on notes" | software | l | **multi-level** tree (CLAWP-064 in-document `parent_ref`): root -> A -> A.1, plus sibling B; each leaf with rubric + scope/out_of_scope/stop/delegability; whole tree promotes atomically |

## Validation transcript (live CLI, 2026-06-12)

### 1. Software dry-run — validates

```
$ cat software-autosave.emit.json | clawpm tasks emit-tree --dry-run
{ "status": "ok",
  "message": "Dry-run complete for project 'planner-demo': 3 leaf(ves) would be emitted under PLANN-001. No writes performed.",
  "data": { "root_id": "PLANN-001", "baseline_ref": "ts:2026-06-12T...", "rejected": [], "constitution_violations": [], "dry_run": true } }
```

### 2. Knowledge-work dry-run — validates (project-agnostic, no code-audit assumptions)

```
$ cat knowledge-competitor-brief.emit.json | clawpm tasks emit-tree --dry-run
{ "status": "ok",
  "message": "Dry-run complete for project 'planner-demo': 3 leaf(ves) would be emitted under PLANN-001. No writes performed.",
  "data": { "root_id": "PLANN-001", "rejected": [], "constitution_violations": [], "dry_run": true } }
```

### 3. Software real emit — parent + 4 tasks + linked PRD

```
$ cat software-autosave.emit.json | clawpm tasks emit-tree
{ "status": "ok",
  "message": "Emitted 4 task(s) under PLANN-001 [PRD: planner-demo-research-prd-prd-draft-autosave-for-the-note-editor]", ... }
```

Each leaf on disk carries the full contract:

```yaml
id: PLANN-001-001
delegability: agent
agent_profile: frontend
leaf_key: autosave::debounced-client-save
scope: [src/editor/autosave/**, src/editor/Editor.tsx]
out_of_scope: [src/sync/**, version history]
stop_conditions: ["If the notes PUT endpoint is not idempotent, STOP and report ..."]
baseline_ref: ts:2026-06-12T...
predictions:
  success_criteria:
  - {criterion: "Edits persist after a 2s idle ...", gradeable_signal: "...", comparator: "eq:1 draft row"}
```

PRD research entry written + linked: `.project/research/2026-06-12_prd-prd-draft-autosave-for-the-note-editor.md`.

### 4. Idempotent re-emit via `attach_to` — leaf_key dedup proven

```
$ cat software-autosave.reemit-attach.json | clawpm tasks emit-tree
{ "status": "ok",
  "message": "Emitted 1 task(s) under PLANN-001", ... }   # only L4 (new); L1 skipped — leaf_key already present
```

**Idempotency caveat (important, governs the skill's re-emit guidance):** dedup is
keyed on `leaf_key` **scanned under the resolved parent**. It therefore works only
when you re-emit against a *stable* parent via `root.attach_to: <root-id>`.
Re-emitting a **new-root** document mints a *fresh* root each time (PLANN-001 then
PLANN-002), so its children can't dedup. The skill's handoff stage records the
emitted root id and re-emits via `attach_to` — never a fresh new-root — which is
why the skill is idempotent.

### 5. Constitution gate — rubric-less leaf reported back

```
$ clawpm constitution add -n requires-rubric -k require_success_criteria
$ echo '{... leaf with no success_criteria ...}' | clawpm tasks emit-tree --dry-run
  "constitution_violations": [
    { "invariant": "requires-rubric", "leaf_ref": "BAD",
      "reason": "Leaf 'BAD' has no success_criteria (invariant: 'requires-rubric')" } ]
```

### 6. Reject-ledger dedup — won't-do match dropped + rationale surfaced

```
$ clawpm tasks state PLANN-003 rejected --rationale "out of scope: OT/CRDT not justified"
$ echo '{... leaf titled "Add real-time multi-device sync" ...}' | clawpm tasks emit-tree --dry-run
  "rejected": [
    { "leaf_ref": "DUP", "leaf_title": "Add real-time multi-device sync",
      "matched_rejected_id": "PLANN-003",
      "rationale": "out of scope: OT/CRDT not justified" } ]
```

The skill's **vet** stage does the *fuzzy/resembling* match against the ledger
before emit; core does the final **exact** match as a backstop and reports it.

### 7. Multi-level emit (CLAWP-064 in-document nesting) — depth-3 tree, atomic

Satisfies success-criterion 3's multi-level case with real evidence. CLAWP-064
landed in-document nesting: a leaf whose `parent_ref` matches another leaf's `ref`
becomes that leaf's child (arbitrary depth; IDs `ROOT-NNN-MMM`; the whole tree
promotes atomically). `software-multilevel.emit.json` declares root ->
A (`ref:"A"`) -> A.1 (`ref:"A1", parent_ref:"A"`), plus sibling B
(`ref:"B", parent_ref:null`).

```
$ cat software-multilevel.emit.json | clawpm tasks emit-tree --dry-run
{ "status": "ok",
  "message": "Dry-run complete for project 'planner-demo': 3 leaf(ves) would be emitted under PLANN-001. No writes performed.",
  "data": { "root_id": "PLANN-001", "rejected": [], "constitution_violations": [], "dry_run": true } }

$ cat software-multilevel.emit.json | clawpm tasks emit-tree
{ "status": "ok",
  "message": "Emitted 4 task(s) under PLANN-001 [PRD: planner-demo-research-prd-prd-collaborative-comments-on-notes]", ... }
```

On-disk structure proves the 3 levels (A is a *directory* task because it has a
nested child; A.1 sits under it):

```
PLANN-001/_task.md                                  # root  — children: [PLANN-001-001, PLANN-001-002]
PLANN-001/PLANN-001-001/_task.md                    # A     — parent: PLANN-001,     children: [PLANN-001-001-001]
PLANN-001/PLANN-001-001/PLANN-001-001-001.md        # A.1   — parent: PLANN-001-001  (depth 3)
PLANN-001/PLANN-001-002.md                          # B     — parent: PLANN-001       (sibling, flat)
```

Parent links verified at every level from the promoted frontmatter:

```yaml
# root PLANN-001/_task.md
id: PLANN-001
children: [PLANN-001-001, PLANN-001-002]
prd_ref: planner-demo-research-prd-prd-collaborative-comments-on-notes

# A  PLANN-001-001/_task.md
id: PLANN-001-001
parent: PLANN-001
children: [PLANN-001-001-001]
leaf_key: comments::threads-milestone   # full contract: scope/out_of_scope/stop/delegability/rubric

# A.1  PLANN-001-001/PLANN-001-001-001.md
id: PLANN-001-001-001
parent: PLANN-001-001                    # nested under A, not the root
leaf_key: comments::reply-nesting

# B  PLANN-001-002.md
id: PLANN-001-002
parent: PLANN-001                        # sibling of A at level 1
leaf_key: comments::notifications-milestone
```

**Note on the depth knob:** this single-call multi-level emit is the CLAWP-064
in-document path (preferred when you're planning the whole tree at once). The
layered `attach_to` path documented in `references/scale-dial.md` remains valid and
is the right tool for *growing* an existing tree across separate planning passes
(and for idempotent re-emit — see section 4).
