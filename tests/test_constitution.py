"""Tests for CLAWP-057 — constitution / governing-principles layer.

Success criteria coverage:
  SC1: clawpm runs identically when no constitution is declared (validate -> [])
  SC2: require_success_criteria invariant flags a no-SC leaf in
       EmitResult.constitution_violations (report-back), and hard-fails under --strict
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.emit_tree import (
    EmitTreeDocument,
    LeafSpec,
    RootSpec,
    PrdSpec,
    EmitResult,
    emit_tree,
    parse_emit_document,
    EmitValidationError,
)
from clawpm.models import (
    Predictions,
    PortfolioConfig,
    SuccessCriterion,
    TaskComplexity,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    """Temporary portfolio with one project, no repo (timestamp baselines)."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_constitution_test_")
    portfolio_root = Path(temp_dir)

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\nstatus = \"active\"\n"
    )

    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    project_dir = projects_dir / "const-test"
    project_dir.mkdir()
    meta = project_dir / ".project"
    meta.mkdir()

    (meta / "settings.toml").write_text(
        'id = "consttest"\nname = "Constitution Test"\nstatus = "active"\npriority = 3\n'
    )
    tasks_dir = meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()
    (tasks_dir / "rejected").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)

    yield {
        "root": portfolio_root,
        "project_dir": project_dir,
        "tasks_dir": tasks_dir,
        "meta": meta,
        "config": load_portfolio_config(portfolio_root),
        "project_id": "consttest",
    }

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir)


def _make_leaf(ref: str, title: str, with_sc: bool = True) -> dict:
    """Return a minimal valid leaf dict, optionally with success_criteria."""
    leaf: dict = {
        "ref": ref,
        "title": title,
        "leaf_key": f"const-test-{ref}",
    }
    if with_sc:
        leaf["success_criteria"] = [
            {
                "criterion": "Tests pass",
                "gradeable_signal": "pytest exit 0",
                "comparator": "eq:0",
            }
        ]
    return leaf


def _make_doc(leaves: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "root": {"title": "Root for constitution test"},
        "leaves": leaves,
    }


# ---------------------------------------------------------------------------
# constitution.py API tests (unit -- no filesystem)
# ---------------------------------------------------------------------------


class TestConstitutionValidate:
    """Direct tests of constitution.validate() API."""

    def test_validate_returns_empty_when_no_constitution_file(self, temp_portfolio):
        """SC1: no constitution file -> validate returns [] (no-op)."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]

        doc = parse_emit_document(_make_doc([_make_leaf("L1", "Leaf one")]))
        violations = constitution.validate(config, project_id, doc)
        assert violations == []

    def test_validate_returns_empty_for_all_good_leaves(self, temp_portfolio):
        """With constitution active and all leaves passing -- no violations."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]
        meta = temp_portfolio["meta"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: require_success_criteria\n"
            "    kind: require_success_criteria\n"
        )

        doc = parse_emit_document(_make_doc([
            _make_leaf("L1", "Leaf one", with_sc=True),
            _make_leaf("L2", "Leaf two", with_sc=True),
        ]))
        violations = constitution.validate(config, project_id, doc)
        assert violations == []

    def test_require_success_criteria_flags_leaf_without_sc(self, temp_portfolio):
        """A leaf with no success_criteria -> violation with require_success_criteria."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]
        meta = temp_portfolio["meta"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: require_success_criteria\n"
            "    kind: require_success_criteria\n"
        )

        doc = parse_emit_document(_make_doc([
            _make_leaf("L1", "Leaf one", with_sc=False),
        ]))
        violations = constitution.validate(config, project_id, doc)

        assert len(violations) == 1
        v = violations[0]
        assert v["invariant"] == "require_success_criteria"
        assert v["leaf_ref"] == "L1"
        assert "reason" in v

    def test_require_success_criteria_flags_only_failing_leaves(self, temp_portfolio):
        """Only leaves missing SC are flagged; compliant leaves are not."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]
        meta = temp_portfolio["meta"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: require_success_criteria\n"
            "    kind: require_success_criteria\n"
        )

        doc = parse_emit_document(_make_doc([
            _make_leaf("L1", "Good leaf", with_sc=True),
            _make_leaf("L2", "Bad leaf", with_sc=False),
            _make_leaf("L3", "Also good", with_sc=True),
        ]))
        violations = constitution.validate(config, project_id, doc)

        refs = [v["leaf_ref"] for v in violations]
        assert refs == ["L2"]

    def test_max_complexity_invariant(self, temp_portfolio):
        """max_complexity invariant flags a leaf exceeding the threshold."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]
        meta = temp_portfolio["meta"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: no_xl_leaves\n"
            "    kind: max_complexity\n"
            "    params:\n"
            "      max: l\n"
        )

        leaf_dict = _make_leaf("L1", "Big leaf", with_sc=True)
        leaf_dict["predictions"] = {"complexity": "xl"}

        doc = parse_emit_document(_make_doc([leaf_dict]))
        violations = constitution.validate(config, project_id, doc)

        assert len(violations) == 1
        v = violations[0]
        assert v["invariant"] == "no_xl_leaves"
        assert v["leaf_ref"] == "L1"

    def test_max_complexity_invariant_passes_within_limit(self, temp_portfolio):
        """max_complexity invariant passes when complexity is within limit."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]
        meta = temp_portfolio["meta"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: no_xl_leaves\n"
            "    kind: max_complexity\n"
            "    params:\n"
            "      max: l\n"
        )

        leaf_dict = _make_leaf("L1", "Medium leaf", with_sc=True)
        leaf_dict["predictions"] = {"complexity": "m"}

        doc = parse_emit_document(_make_doc([leaf_dict]))
        violations = constitution.validate(config, project_id, doc)
        assert violations == []

    def test_require_scope_invariant(self, temp_portfolio):
        """require_scope invariant flags a leaf with empty scope."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]
        meta = temp_portfolio["meta"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: require_scope\n"
            "    kind: require_scope\n"
        )

        doc = parse_emit_document(_make_doc([_make_leaf("L1", "No scope leaf", with_sc=True)]))
        violations = constitution.validate(config, project_id, doc)

        assert len(violations) == 1
        v = violations[0]
        assert v["invariant"] == "require_scope"
        assert v["leaf_ref"] == "L1"

    def test_require_scope_passes_when_scope_set(self, temp_portfolio):
        """require_scope invariant passes when leaf has scope entries."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]
        meta = temp_portfolio["meta"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: require_scope\n"
            "    kind: require_scope\n"
        )

        leaf_dict = _make_leaf("L1", "Scoped leaf", with_sc=True)
        leaf_dict["scope"] = ["src/**/*.py"]

        doc = parse_emit_document(_make_doc([leaf_dict]))
        violations = constitution.validate(config, project_id, doc)
        assert violations == []

    def test_advisory_invariant_stored_as_info_only(self, temp_portfolio):
        """Advisory invariants are stored and returned as info-only (never block)."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]
        meta = temp_portfolio["meta"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: cite_sources\n"
            "    kind: advisory\n"
            "    description: 'Knowledge-work deliverables must cite sources'\n"
        )

        doc = parse_emit_document(_make_doc([_make_leaf("L1", "Leaf one", with_sc=True)]))
        violations = constitution.validate(config, project_id, doc)

        # Advisory invariants are returned once as info-level, not per-leaf
        assert len(violations) == 1
        v = violations[0]
        assert v["invariant"] == "cite_sources"
        assert v.get("level") == "advisory"

    def test_multiple_invariants_multiple_violations(self, temp_portfolio):
        """Multiple active invariants can each generate violations."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]
        meta = temp_portfolio["meta"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: require_success_criteria\n"
            "    kind: require_success_criteria\n"
            "  - name: require_scope\n"
            "    kind: require_scope\n"
        )

        doc = parse_emit_document(_make_doc([_make_leaf("L1", "Bare leaf", with_sc=False)]))
        violations = constitution.validate(config, project_id, doc)

        invariant_names = {v["invariant"] for v in violations}
        assert "require_success_criteria" in invariant_names
        assert "require_scope" in invariant_names

    def test_no_constitution_file_is_exact_no_op(self, temp_portfolio):
        """SC1: when no constitution.yaml exists, validate() is an exact no-op."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]
        meta = temp_portfolio["meta"]

        const_file = meta / "constitution.yaml"
        assert not const_file.exists()

        doc = parse_emit_document(_make_doc([_make_leaf("L1", "Leaf", with_sc=False)]))
        violations = constitution.validate(config, project_id, doc)
        assert violations == []

    def test_malformed_constitution_file_is_fail_open(self, temp_portfolio):
        """A malformed constitution.yaml is fail-open (returns [], does not crash)."""
        from clawpm import constitution

        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]
        meta = temp_portfolio["meta"]

        (meta / "constitution.yaml").write_text("this: is: not: valid: yaml: {{{")

        doc = parse_emit_document(_make_doc([_make_leaf("L1", "Leaf", with_sc=True)]))
        violations = constitution.validate(config, project_id, doc)
        assert violations == []


# ---------------------------------------------------------------------------
# emit_tree integration tests (SC2)
# ---------------------------------------------------------------------------


class TestConstitutionViaEmitTree:
    """SC2: violations flow through emit_tree into EmitResult."""

    def test_violations_in_result_report_back(self, temp_portfolio):
        """SC2: constitution violations appear in EmitResult.constitution_violations."""
        meta = temp_portfolio["meta"]
        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: require_success_criteria\n"
            "    kind: require_success_criteria\n"
        )

        doc = parse_emit_document(_make_doc([
            _make_leaf("L1", "No SC leaf", with_sc=False),
        ]))

        result = emit_tree(config, project_id, doc, strict=False)

        assert len(result.constitution_violations) >= 1
        refs = [v["leaf_ref"] for v in result.constitution_violations]
        assert "L1" in refs
        # Emission still proceeds (report-back mode)
        assert len(result.emitted) >= 1

    def test_violations_hard_fail_under_strict(self, temp_portfolio):
        """SC2: --strict causes EmitValidationError when violations exist."""
        meta = temp_portfolio["meta"]
        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: require_success_criteria\n"
            "    kind: require_success_criteria\n"
        )

        doc = parse_emit_document(_make_doc([
            _make_leaf("L1", "No SC leaf", with_sc=False),
        ]))

        with pytest.raises(EmitValidationError, match="constitution"):
            emit_tree(config, project_id, doc, strict=True)

    def test_no_violations_no_constitution_file(self, temp_portfolio):
        """SC1 via emit_tree: no constitution file -> EmitResult.constitution_violations == []."""
        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]

        doc = parse_emit_document(_make_doc([
            _make_leaf("L1", "Good leaf", with_sc=False),
        ]))

        result = emit_tree(config, project_id, doc, strict=False)
        assert result.constitution_violations == []

    def test_no_violations_when_leaf_complies(self, temp_portfolio):
        """Compliant leaves produce no violations even with constitution active."""
        meta = temp_portfolio["meta"]
        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: require_success_criteria\n"
            "    kind: require_success_criteria\n"
        )

        doc = parse_emit_document(_make_doc([
            _make_leaf("L1", "Good leaf", with_sc=True),
        ]))

        result = emit_tree(config, project_id, doc, strict=False)
        assert result.constitution_violations == []
        assert len(result.emitted) >= 1

    def test_advisory_invariant_does_not_block_strict(self, temp_portfolio):
        """SC: an ACTIVE advisory invariant must NEVER block --strict emission.

        The advisory entry IS surfaced in result.constitution_violations
        (report-back/info) but does not raise under strict=True.
        """
        meta = temp_portfolio["meta"]
        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: cite_sources\n"
            "    kind: advisory\n"
            "    description: 'Knowledge-work deliverables must cite sources'\n"
        )

        doc = parse_emit_document(_make_doc([
            _make_leaf("L1", "Good leaf", with_sc=True),
        ]))

        # strict=True must NOT raise just because an advisory invariant is active.
        result = emit_tree(config, project_id, doc, strict=True)

        # Emission succeeded and the advisory entry is surfaced (not blocking).
        assert len(result.emitted) >= 1
        advisory = [
            v for v in result.constitution_violations
            if v.get("level") == "advisory"
        ]
        assert len(advisory) == 1
        assert advisory[0]["invariant"] == "cite_sources"

    def test_advisory_plus_codecheck_violation_blocks_strict(self, temp_portfolio):
        """A code-checkable violation still blocks --strict even alongside advisory.

        Confirms the strict filter excludes ONLY advisory entries, not real
        code-checkable violations.
        """
        meta = temp_portfolio["meta"]
        config = temp_portfolio["config"]
        project_id = temp_portfolio["project_id"]

        (meta / "constitution.yaml").write_text(
            "invariants:\n"
            "  - name: cite_sources\n"
            "    kind: advisory\n"
            "  - name: require_success_criteria\n"
            "    kind: require_success_criteria\n"
        )

        doc = parse_emit_document(_make_doc([
            _make_leaf("L1", "No SC leaf", with_sc=False),
        ]))

        with pytest.raises(EmitValidationError, match="constitution"):
            emit_tree(config, project_id, doc, strict=True)


# ---------------------------------------------------------------------------
# CLI tests (clawpm constitution add/list/remove)
# ---------------------------------------------------------------------------


class TestConstitutionCLI:
    """Tests for the `clawpm constitution` command group."""

    def test_constitution_add_creates_file(self, temp_portfolio):
        """constitution add writes an invariant to constitution.yaml."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "constitution", "add",
            "--project", "consttest",
            "--name", "require_success_criteria",
            "--kind", "require_success_criteria",
        ])
        assert result.exit_code == 0, result.output

        const_file = temp_portfolio["meta"] / "constitution.yaml"
        assert const_file.exists()
        data = yaml.safe_load(const_file.read_text())
        names = [inv["name"] for inv in data["invariants"]]
        assert "require_success_criteria" in names

    def test_constitution_add_idempotent(self, temp_portfolio):
        """Adding the same invariant twice does not duplicate it."""
        runner = CliRunner()
        for _ in range(2):
            result = runner.invoke(main, [
                "constitution", "add",
                "--project", "consttest",
                "--name", "require_success_criteria",
                "--kind", "require_success_criteria",
            ])
            assert result.exit_code == 0, result.output

        const_file = temp_portfolio["meta"] / "constitution.yaml"
        data = yaml.safe_load(const_file.read_text())
        names = [inv["name"] for inv in data["invariants"]]
        assert names.count("require_success_criteria") == 1

    def test_constitution_list_empty(self, temp_portfolio):
        """constitution list with no file returns empty list."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "--format", "json",
            "constitution", "list",
            "--project", "consttest",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        invariants = data if isinstance(data, list) else data.get("invariants", [])
        assert invariants == []

    def test_constitution_list_shows_invariants(self, temp_portfolio):
        """constitution list returns invariants after add."""
        runner = CliRunner()
        runner.invoke(main, [
            "constitution", "add",
            "--project", "consttest",
            "--name", "require_success_criteria",
            "--kind", "require_success_criteria",
        ])

        result = runner.invoke(main, [
            "--format", "json",
            "constitution", "list",
            "--project", "consttest",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        invariants = data if isinstance(data, list) else data.get("invariants", [])
        names = [inv["name"] for inv in invariants]
        assert "require_success_criteria" in names

    def test_constitution_remove(self, temp_portfolio):
        """constitution remove deletes the named invariant."""
        runner = CliRunner()
        for name, kind in [
            ("require_success_criteria", "require_success_criteria"),
            ("require_scope", "require_scope"),
        ]:
            runner.invoke(main, [
                "constitution", "add",
                "--project", "consttest",
                "--name", name,
                "--kind", kind,
            ])

        result = runner.invoke(main, [
            "constitution", "remove",
            "--project", "consttest",
            "--name", "require_scope",
        ])
        assert result.exit_code == 0, result.output

        const_file = temp_portfolio["meta"] / "constitution.yaml"
        data = yaml.safe_load(const_file.read_text())
        names = [inv["name"] for inv in data["invariants"]]
        assert "require_scope" not in names
        assert "require_success_criteria" in names

    def test_constitution_add_advisory(self, temp_portfolio):
        """constitution add with kind=advisory stores the description."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "constitution", "add",
            "--project", "consttest",
            "--name", "cite_sources",
            "--kind", "advisory",
            "--description", "Knowledge-work deliverables must cite sources",
        ])
        assert result.exit_code == 0, result.output

        const_file = temp_portfolio["meta"] / "constitution.yaml"
        data = yaml.safe_load(const_file.read_text())
        inv = next(i for i in data["invariants"] if i["name"] == "cite_sources")
        assert inv["kind"] == "advisory"

    def test_constitution_add_max_complexity_with_params(self, temp_portfolio):
        """constitution add with kind=max_complexity stores params."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "constitution", "add",
            "--project", "consttest",
            "--name", "no_xl",
            "--kind", "max_complexity",
            "--param", "max=l",
        ])
        assert result.exit_code == 0, result.output

        const_file = temp_portfolio["meta"] / "constitution.yaml"
        data = yaml.safe_load(const_file.read_text())
        inv = next(i for i in data["invariants"] if i["name"] == "no_xl")
        assert inv["params"]["max"] == "l"

    def test_constitution_remove_nonexistent_is_graceful(self, temp_portfolio):
        """Removing a nonexistent invariant exits 0 (idempotent)."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "constitution", "remove",
            "--project", "consttest",
            "--name", "doesnt_exist",
        ])
        assert result.exit_code == 0, result.output
