"""Tests for CLAWP-111-003 — emit-tree can emit a decision map.

Covers the four new document keys:
  * leaf ``kind`` ("build" | "decision")
  * leaf ``depends_refs`` (leaf refs, or ``id:<existing-task-id>``)
  * root ``destination`` / ``not_yet_specified``

plus the round-trip through ``tasks add``, ``tasks edit``, ``tasks fog`` and
``emit-tree`` for every new field (parent CLAWP-111 pre_mortem), and the
planner example validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm import emit_tree as _et
from clawpm.emit_tree import emit_tree, parse_emit_document
from clawpm.models import Task
from clawpm.tasks import add_task, get_task, list_tasks

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "skills" / "clawpm-planner" / "examples"


def _leaf(ref: str, **extra) -> dict:
    leaf = {"ref": ref, "parent_ref": None, "title": f"Leaf {ref}", "leaf_key": f"dm-{ref}"}
    leaf.update(extra)
    return leaf


def _doc(leaves: list[dict], root: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "root": root or {"title": "Decision map root"},
        "leaves": leaves,
    }


def _all_md(tasks_dir: Path) -> list[Path]:
    return sorted(p for p in tasks_dir.rglob("*.md"))


def _key_of(task_dict: dict) -> str | None:
    """Task.to_dict() does not expose leaf_key; _leaf() titles are "Leaf <ref>"
    and keys "dm-<ref>", so recover the key from the title."""
    title = task_dict.get("title") or ""
    return f"dm-{title[len('Leaf '):]}" if title.startswith("Leaf ") else None


def _by_leaf_key(result) -> dict[str, dict]:
    return {_key_of(t): t for t in result.emitted if _key_of(t)}


def _emit(iso, doc: dict, **kw):
    return emit_tree(iso.config, iso.project_id, parse_emit_document(doc), **kw)


def _emitted_tasks(iso, result) -> dict[str, Task]:
    """Reload every emitted task from disk, keyed by leaf_key (root keyed 'root')."""
    out: dict[str, Task] = {}
    for t in result.emitted:
        task = get_task(iso.config, iso.project_id, t["id"])
        assert task is not None, t["id"]
        out[_key_of(t) or "root"] = task
    return out


class TestParse:
    def test_new_keys_accepted(self):
        doc = parse_emit_document(
            _doc(
                [
                    _leaf("A", kind="decision"),
                    _leaf("B", depends_refs=["A"]),
                ],
                root={
                    "title": "Map",
                    "destination": "A shipped, documented importer",
                    "not_yet_specified": ["error UX", "retry policy"],
                },
            )
        )
        assert doc.root.destination == "A shipped, documented importer"
        assert doc.root.not_yet_specified == ["error UX", "retry policy"]
        assert doc.leaves[0].kind == "decision"
        assert doc.leaves[1].kind == "build"
        assert doc.leaves[1].depends_refs == ["A"]

    def test_invalid_kind_rejected(self):
        with pytest.raises(_et.EmitValidationError, match="kind"):
            parse_emit_document(_doc([_leaf("A", kind="spike")]))

    def test_depends_refs_must_be_list_of_strings(self):
        with pytest.raises(_et.EmitValidationError, match="depends_refs"):
            parse_emit_document(_doc([_leaf("A", depends_refs="B")]))
        with pytest.raises(_et.EmitValidationError, match="depends_refs"):
            parse_emit_document(_doc([_leaf("A", depends_refs=[1])]))

    def test_unknown_depends_ref_rejected(self):
        with pytest.raises(_et.EmitValidationError, match="NOPE"):
            parse_emit_document(_doc([_leaf("A", depends_refs=["NOPE"])]))

    def test_self_dependency_rejected(self):
        with pytest.raises(_et.EmitValidationError, match="itself"):
            parse_emit_document(_doc([_leaf("A", depends_refs=["A"])]))

    def test_two_node_cycle_rejected(self):
        with pytest.raises(_et.EmitValidationError, match="[Cc]ycle"):
            parse_emit_document(
                _doc([_leaf("A", depends_refs=["B"]), _leaf("B", depends_refs=["A"])])
            )

    def test_depending_on_own_ancestor_rejected(self):
        # A child that depends on its parent deadlocks: the parent's rollup
        # waits on its children.
        with pytest.raises(_et.EmitValidationError, match="ancestor"):
            parse_emit_document(
                _doc(
                    [
                        _leaf("P"),
                        _leaf("C", parent_ref="P", depends_refs=["P"]),
                    ]
                )
            )

    def test_id_prefixed_entry_is_not_a_leaf_ref(self):
        doc = parse_emit_document(_doc([_leaf("A", depends_refs=["id:TEST-009"])]))
        assert doc.leaves[0].depends_refs == ["id:TEST-009"]

    def test_leaf_ref_with_id_prefix_rejected(self):
        with pytest.raises(_et.EmitValidationError, match="id:"):
            parse_emit_document(_doc([_leaf("id:A")]))

    def test_destination_must_be_string(self):
        with pytest.raises(_et.EmitValidationError, match="destination"):
            parse_emit_document(_doc([_leaf("A")], root={"title": "R", "destination": 3}))

    def test_not_yet_specified_must_be_list_of_strings(self):
        with pytest.raises(_et.EmitValidationError, match="not_yet_specified"):
            parse_emit_document(
                _doc([_leaf("A")], root={"title": "R", "not_yet_specified": "fog"})
            )

    def test_map_keys_rejected_under_attach_to(self):
        with pytest.raises(_et.EmitValidationError, match="attach_to"):
            parse_emit_document(
                _doc([_leaf("A")], root={"attach_to": "TEST-001", "destination": "x"})
            )


class TestEmitPersistence:
    def test_kind_and_depends_resolved_to_minted_ids(self, isolated_portfolio):
        iso = isolated_portfolio
        result = _emit(
            iso,
            _doc(
                [
                    _leaf("A", kind="decision"),
                    _leaf("B", depends_refs=["A"]),
                    _leaf("C", depends_refs=["A", "B"]),
                ]
            ),
        )
        tasks = _emitted_tasks(iso, result)
        a, b, c = tasks["dm-A"], tasks["dm-B"], tasks["dm-C"]
        assert a.kind == "decision"
        assert b.kind == "build" and c.kind == "build"
        assert a.depends == []
        assert b.depends == [a.id]
        assert c.depends == [a.id, b.id]

    def test_build_kind_is_omitted_from_frontmatter(self, isolated_portfolio):
        iso = isolated_portfolio
        result = _emit(iso, _doc([_leaf("A", kind="build")]))
        text = tasks_file_text(iso, result, "dm-A")
        assert "kind:" not in text
        assert "depends:" not in text

    def test_depends_across_parent_levels(self, isolated_portfolio):
        iso = isolated_portfolio
        result = _emit(
            iso,
            _doc(
                [
                    _leaf("P"),
                    _leaf("C", parent_ref="P", kind="decision"),
                    _leaf("D", depends_refs=["C"]),
                ]
            ),
        )
        tasks = _emitted_tasks(iso, result)
        assert tasks["dm-D"].depends == [tasks["dm-C"].id]
        assert tasks["dm-C"].id.startswith(tasks["dm-P"].id + "-")

    def test_duplicate_depends_refs_deduped_in_order(self, isolated_portfolio):
        iso = isolated_portfolio
        result = _emit(
            iso, _doc([_leaf("A"), _leaf("B"), _leaf("C", depends_refs=["B", "A", "B"])])
        )
        tasks = _emitted_tasks(iso, result)
        assert tasks["dm-C"].depends == [tasks["dm-B"].id, tasks["dm-A"].id]

    def test_root_destination_and_fog_persist(self, isolated_portfolio):
        iso = isolated_portfolio
        result = _emit(
            iso,
            _doc(
                [_leaf("A")],
                root={
                    "title": "Map",
                    "destination": "Importer shipped",
                    "not_yet_specified": ["retry policy", "error UX"],
                },
            ),
        )
        root = _emitted_tasks(iso, result)["root"]
        assert root.destination == "Importer shipped"
        assert root.not_yet_specified == ["retry policy", "error UX"]

    def test_plain_doc_frontmatter_unchanged(self, isolated_portfolio):
        iso = isolated_portfolio
        result = _emit(iso, _doc([_leaf("A")]))
        for t in _emitted_tasks(iso, result).values():
            text = t.file_path.read_text(encoding="utf-8")
            for key in ("kind:", "depends:", "destination:", "not_yet_specified:"):
                assert key not in text

    def test_attach_to_existing_id_dependency(self, isolated_portfolio):
        iso = isolated_portfolio
        existing = add_task(iso.config, iso.project_id, "Existing blocker")
        dep = add_task(iso.config, iso.project_id, "Attach root")
        result = _emit(
            iso,
            _doc(
                [_leaf("A", depends_refs=[f"id:{existing.id}"])],
                root={"attach_to": dep.id},
            ),
        )
        tasks = _emitted_tasks(iso, result)
        assert tasks["dm-A"].depends == [existing.id]

    def test_mixed_ref_and_id_dependencies(self, isolated_portfolio):
        iso = isolated_portfolio
        existing = add_task(iso.config, iso.project_id, "Existing blocker")
        result = _emit(
            iso,
            _doc(
                [_leaf("A"), _leaf("B", depends_refs=["A", f"id:{existing.id}"])]
            ),
        )
        tasks = _emitted_tasks(iso, result)
        assert tasks["dm-B"].depends == [tasks["dm-A"].id, existing.id]

    def test_reemit_resolves_dependency_to_already_emitted_leaf(self, isolated_portfolio):
        iso = isolated_portfolio
        first = _emit(iso, _doc([_leaf("A", kind="decision")]))
        root_id = first.root_id
        a_id = _by_leaf_key(first)["dm-A"]["id"]
        second = _emit(
            iso,
            _doc(
                [_leaf("A", kind="decision"), _leaf("B", depends_refs=["A"])],
                root={"attach_to": root_id},
            ),
        )
        assert [_key_of(t) for t in second.emitted] == ["dm-B"]
        b = get_task(iso.config, iso.project_id, _by_leaf_key(second)["dm-B"]["id"])
        assert b.depends == [a_id]


def tasks_file_text(iso, result, leaf_key: str) -> str:
    tid = _by_leaf_key(result)[leaf_key]["id"]
    task = get_task(iso.config, iso.project_id, tid)
    return task.file_path.read_text(encoding="utf-8")


class TestGateFailuresWriteNothing:
    def _snapshot(self, iso):
        return _all_md(iso.tasks_dir)

    def test_unknown_id_dependency_fails_dry_run_and_real(self, isolated_portfolio):
        iso = isolated_portfolio
        before = self._snapshot(iso)
        doc = parse_emit_document(_doc([_leaf("A", depends_refs=["id:TEST-404"])]))
        for dry in (True, False):
            with pytest.raises(_et.EmitValidationError, match="TEST-404"):
                emit_tree(iso.config, iso.project_id, doc, dry_run=dry)
        assert self._snapshot(iso) == before
        assert not [p for p in iso.tasks_dir.iterdir() if p.name.startswith(".emit-")]

    def test_dependency_on_rejected_leaf_fails_loud(self, isolated_portfolio):
        iso = isolated_portfolio
        from clawpm.models import TaskState
        from clawpm.tasks import change_task_state

        old = add_task(iso.config, iso.project_id, "Leaf A")
        change_task_state(
            iso.config, iso.project_id, old.id, TaskState.REJECTED, rationale="no"
        )
        before = self._snapshot(iso)
        doc = parse_emit_document(
            _doc(
                [
                    {**_leaf("A"), "title": "Leaf A"},
                    _leaf("B", depends_refs=["A"]),
                ]
            )
        )
        with pytest.raises(_et.EmitValidationError, match="rejected"):
            emit_tree(iso.config, iso.project_id, doc)
        assert self._snapshot(iso) == before

    def test_cli_unknown_ref_and_cycle_fail_validation_no_writes(self, isolated_portfolio):
        iso = isolated_portfolio
        before = self._snapshot(iso)
        runner = CliRunner()
        bad_docs = [
            _doc([_leaf("A", depends_refs=["GHOST"])]),
            _doc([_leaf("A", depends_refs=["B"]), _leaf("B", depends_refs=["A"])]),
        ]
        for doc in bad_docs:
            for extra in ([], ["--dry-run"]):
                res = runner.invoke(
                    main,
                    ["--project", iso.project_id, "tasks", "emit-tree", *extra],
                    input=json.dumps(doc),
                )
                assert res.exit_code == 1, res.output
                assert json.loads(res.output)["error"] == "validation_error"
        assert self._snapshot(iso) == before


class TestRoundTrip:
    """Every new field survives tasks add / tasks edit / tasks fog / emit-tree."""

    def test_emitted_kind_matches_tasks_add_kind_shape(self, isolated_portfolio):
        iso = isolated_portfolio
        added = add_task(iso.config, iso.project_id, "Via add", kind="decision")
        emitted = _emit(iso, _doc([_leaf("A", kind="decision")]))
        e = get_task(iso.config, iso.project_id, _by_leaf_key(emitted)["dm-A"]["id"])
        assert added.kind == e.kind == "decision"
        # Same on-disk spelling of the field.
        assert "kind: decision" in added.file_path.read_text(encoding="utf-8")
        assert "kind: decision" in e.file_path.read_text(encoding="utf-8")

    def test_emitted_depends_matches_tasks_add_depends_shape(self, isolated_portfolio):
        iso = isolated_portfolio
        base = add_task(iso.config, iso.project_id, "Base")
        added = add_task(iso.config, iso.project_id, "Via add", depends=[base.id])
        emitted = _emit(
            iso, _doc([_leaf("A"), _leaf("B", depends_refs=["A", f"id:{base.id}"])])
        )
        e = get_task(iso.config, iso.project_id, _by_leaf_key(emitted)["dm-B"]["id"])
        assert added.depends == [base.id]
        assert e.depends[-1] == base.id and len(e.depends) == 2

    def test_tasks_edit_preserves_emitted_fields(self, isolated_portfolio):
        iso = isolated_portfolio
        emitted = _emit(
            iso,
            _doc(
                [_leaf("A"), _leaf("B", kind="decision", depends_refs=["A"])],
                root={
                    "title": "Map",
                    "destination": "Shipped",
                    "not_yet_specified": ["fog one"],
                },
            ),
        )
        runner = CliRunner()
        b_id = _by_leaf_key(emitted)["dm-B"]["id"]
        root_id = emitted.root_id
        a_id = _by_leaf_key(emitted)["dm-A"]["id"]

        res = runner.invoke(
            main,
            ["--project", iso.project_id, "tasks", "edit", b_id, "--priority", "2"],
        )
        assert res.exit_code == 0, res.output
        b = get_task(iso.config, iso.project_id, b_id)
        assert b.kind == "decision" and b.depends == [a_id] and b.priority == 2

        res = runner.invoke(
            main,
            ["--project", iso.project_id, "tasks", "edit", root_id, "--priority", "2"],
        )
        assert res.exit_code == 0, res.output
        root = get_task(iso.config, iso.project_id, root_id)
        assert root.destination == "Shipped"
        assert root.not_yet_specified == ["fog one"]

        # kind can be flipped through edit, and depends survive that too.
        res = runner.invoke(
            main,
            ["--project", iso.project_id, "tasks", "edit", a_id, "--kind", "decision"],
        )
        assert res.exit_code == 0, res.output
        assert get_task(iso.config, iso.project_id, a_id).kind == "decision"

    def test_tasks_fog_edits_emitted_fog_list(self, isolated_portfolio):
        iso = isolated_portfolio
        emitted = _emit(
            iso,
            _doc(
                [_leaf("A")],
                root={"title": "Map", "not_yet_specified": ["fog one", "fog two"]},
            ),
        )
        runner = CliRunner()
        res = runner.invoke(
            main,
            ["--project", iso.project_id, "tasks", "fog", emitted.root_id, "--drop", "fog one"],
        )
        assert res.exit_code == 0, res.output
        root = get_task(iso.config, iso.project_id, emitted.root_id)
        assert root.not_yet_specified == ["fog two"]

    def test_graduates_from_emitted_fog_list(self, isolated_portfolio):
        iso = isolated_portfolio
        emitted = _emit(
            iso,
            _doc([_leaf("A")], root={"title": "Map", "not_yet_specified": ["retry policy"]}),
        )
        runner = CliRunner()
        res = runner.invoke(
            main,
            [
                "--project", iso.project_id, "tasks", "add", "--title", "Retry policy",
                "--parent", emitted.root_id, "--graduates", "retry",
            ],
        )
        assert res.exit_code == 0, res.output
        root = get_task(iso.config, iso.project_id, emitted.root_id)
        assert root.not_yet_specified == []

    def test_cli_emit_round_trip_json_envelope(self, isolated_portfolio):
        iso = isolated_portfolio
        runner = CliRunner()
        doc = _doc(
            [_leaf("A", kind="decision"), _leaf("B", depends_refs=["A"])],
            root={"title": "Map", "destination": "Done"},
        )
        res = runner.invoke(
            main, ["--project", iso.project_id, "tasks", "emit-tree"], input=json.dumps(doc)
        )
        assert res.exit_code == 0, res.output
        emitted = {_key_of(t): t for t in json.loads(res.output)["data"]["emitted"] if _key_of(t)}
        assert emitted["dm-A"]["kind"] == "decision"
        assert emitted["dm-B"]["depends"] == [emitted["dm-A"]["id"]]
        # And the same task is reloadable with identical fields (to_dict round trip).
        reloaded = get_task(iso.config, iso.project_id, emitted["dm-B"]["id"])
        assert reloaded.to_dict()["depends"] == emitted["dm-B"]["depends"]


class TestExamples:
    def test_decision_map_example_exists_and_dry_runs(self, isolated_portfolio):
        path = EXAMPLES_DIR / "decision-map.emit.json"
        assert path.is_file()
        raw = json.loads(path.read_text(encoding="utf-8"))
        # The example names the demo project; point it at the fixture's.
        raw["project"] = isolated_portfolio.project_id
        runner = CliRunner()
        res = runner.invoke(
            main,
            ["--project", isolated_portfolio.project_id, "tasks", "emit-tree", "--dry-run"],
            input=json.dumps(raw),
        )
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)["data"]
        assert data["dry_run"] is True

    def test_every_planner_example_parses(self):
        # The re-emit example attaches to a task that does not exist here, so
        # only parse-level validation is asserted for the whole directory.
        files = sorted(EXAMPLES_DIR.glob("*.json"))
        assert files
        for f in files:
            parse_emit_document(json.loads(f.read_text(encoding="utf-8")))

    def test_contract_doc_names_the_new_keys(self):
        text = (
            EXAMPLES_DIR.parent / "references" / "emission-contract.md"
        ).read_text(encoding="utf-8")
        for key in ("depends_refs", "destination", "not_yet_specified", "`kind`"):
            assert key in text, key
