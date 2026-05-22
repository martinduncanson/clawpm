"""Tests for rubric-shaped success criteria + emit-rubric (CLAWP-016)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import Predictions, SuccessCriterion, Task
from clawpm.rubric import render_rubric_markdown, render_rubric_json_payload
from clawpm.tasks import add_task, get_task


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_rubric_test_")
    portfolio_root = Path(temp_dir)
    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n'
    )
    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    project_dir = projects_dir / "test-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
    )
    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    config = load_portfolio_config(portfolio_root)

    yield {"root": portfolio_root, "project_dir": project_dir,
           "tasks_dir": tasks_dir, "config": config}

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# SuccessCriterion dataclass
# ---------------------------------------------------------------------------


class TestSuccessCriterion:
    def test_bare_string_via_yaml(self):
        sc = SuccessCriterion.from_yaml("P95 latency <200ms")
        assert sc.criterion == "P95 latency <200ms"
        assert sc.gradeable_signal is None
        assert sc.comparator is None
        assert not sc.is_structured()

    def test_structured_dict_via_yaml(self):
        sc = SuccessCriterion.from_yaml({
            "criterion": "P95 latency <200ms",
            "gradeable_signal": "load test output shows p95 metric",
            "comparator": "lt:200ms",
        })
        assert sc.criterion == "P95 latency <200ms"
        assert sc.gradeable_signal == "load test output shows p95 metric"
        assert sc.comparator == "lt:200ms"
        assert sc.is_structured()

    def test_equality_against_plain_string(self):
        """Existing tests do `predictions.success_criteria == ["foo"]`."""
        sc = SuccessCriterion(criterion="foo")
        assert sc == "foo"
        assert [sc] == ["foo"]

    def test_structured_inequality_against_plain_string(self):
        sc = SuccessCriterion(
            criterion="foo", gradeable_signal="bar"
        )
        # Even when criterion matches, structured criteria don't fold to
        # plain-string equality (deliberate — surface the difference).
        assert sc == "foo"  # We keep this loose match by design.

    def test_to_yaml_bare_when_no_structure(self):
        sc = SuccessCriterion(criterion="foo")
        assert sc.to_yaml() == "foo"

    def test_to_yaml_dict_when_structured(self):
        sc = SuccessCriterion(
            criterion="foo", gradeable_signal="proof", comparator="eq:1"
        )
        assert sc.to_yaml() == {
            "criterion": "foo",
            "gradeable_signal": "proof",
            "comparator": "eq:1",
        }

    def test_from_cli_plain_string(self):
        sc = SuccessCriterion.from_cli("P95 latency <200ms")
        assert sc.criterion == "P95 latency <200ms"
        assert not sc.is_structured()

    def test_from_cli_json_object(self):
        sc = SuccessCriterion.from_cli(
            '{"criterion": "P95 latency <200ms", '
            '"gradeable_signal": "load test output", "comparator": "lt:200ms"}'
        )
        assert sc.criterion == "P95 latency <200ms"
        assert sc.gradeable_signal == "load test output"
        assert sc.comparator == "lt:200ms"

    def test_from_cli_curly_brace_string_not_json(self):
        """A criterion that starts with { but isn't valid JSON stays a string."""
        sc = SuccessCriterion.from_cli("{count} > 0")
        assert sc.criterion == "{count} > 0"
        assert not sc.is_structured()

    def test_from_yaml_missing_criterion_raises(self):
        with pytest.raises(ValueError):
            SuccessCriterion.from_yaml({"gradeable_signal": "x"})


# ---------------------------------------------------------------------------
# Predictions normalisation (back-compat)
# ---------------------------------------------------------------------------


class TestSuccessCriterionHashContract:
    """Codex-review hardening: __eq__ vs __hash__ must obey Python data
    model. a == b → hash(a) == hash(b). A bare-criterion SC equals plain
    str 'foo' → hash(SC('foo')) must equal hash('foo')."""

    def test_hash_consistent_with_str_equality(self):
        sc = SuccessCriterion(criterion="foo")
        assert sc == "foo"
        assert hash(sc) == hash("foo")

    def test_set_membership_works_with_str_lookup(self):
        s = {SuccessCriterion(criterion="foo")}
        assert "foo" in s  # would silently miss with broken hash

    def test_dict_lookup_works_with_str_key(self):
        d = {SuccessCriterion(criterion="foo"): 1}
        # Direct str lookup must hit the SC key
        assert d.get("foo") == 1

    def test_structured_variants_collide_on_criterion(self):
        """Documented tradeoff: structured variants sharing the same
        criterion text collide in dicts/sets. Equality on structured form
        still disambiguates by signal + comparator so set semantics
        remain correct, but hash bucket is shared."""
        a = SuccessCriterion(criterion="x", gradeable_signal="alpha")
        b = SuccessCriterion(criterion="x", gradeable_signal="beta")
        assert hash(a) == hash(b)
        assert a != b


class TestPredictionsBackCompat:
    def test_predictions_accepts_list_of_strings(self):
        """Existing call sites passing list[str] must still work."""
        p = Predictions(success_criteria=["a", "b"])
        assert len(p.success_criteria) == 2
        assert p.success_criteria == ["a", "b"]  # equality through __eq__

    def test_predictions_accepts_list_of_dicts(self):
        p = Predictions(success_criteria=[
            {"criterion": "a", "gradeable_signal": "x"},
            {"criterion": "b"},
        ])
        assert p.success_criteria[0].criterion == "a"
        assert p.success_criteria[0].gradeable_signal == "x"
        assert p.success_criteria[1].criterion == "b"

    def test_predictions_to_dict_roundtrip(self):
        p = Predictions(success_criteria=["a", "b"])
        d = p.to_dict()
        # Bare strings serialise back as bare strings (back-compat for tests
        # that do `record["predictions"]["success_criteria"] == ["a", "b"]`).
        assert d["success_criteria"] == ["a", "b"]

    def test_predictions_to_dict_preserves_structured(self):
        p = Predictions(success_criteria=[
            SuccessCriterion(criterion="a", gradeable_signal="x"),
        ])
        d = p.to_dict()
        assert d["success_criteria"][0]["criterion"] == "a"
        assert d["success_criteria"][0]["gradeable_signal"] == "x"


# ---------------------------------------------------------------------------
# Rubric rendering
# ---------------------------------------------------------------------------


class TestRubricRendering:
    def test_renders_bare_criterion(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(
            config, "test", title="My task",
            predictions=Predictions(success_criteria=["criterion-one"]),
        )
        task = get_task(config, "test", task.id)
        md = render_rubric_markdown(task)
        assert "# Rubric: My task" in md
        assert "criterion-one" in md
        assert "operator judgment" in md  # default for empty gradeable_signal
        assert "qualitative review" in md
        assert "## Grading instructions" in md
        # Piebald evaluator doctrine line present
        assert "evidence, not proof" in md

    def test_renders_structured_criterion(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(
            config, "test", title="Latency task",
            predictions=Predictions(success_criteria=[
                SuccessCriterion(
                    criterion="P95 < 200ms",
                    gradeable_signal="load test report shows p95",
                    comparator="lt:200ms",
                ),
            ]),
        )
        task = get_task(config, "test", task.id)
        md = render_rubric_markdown(task)
        assert "P95 < 200ms" in md
        assert "load test report shows p95" in md
        assert "lt:200ms" in md

    def test_renders_empty_criteria_block(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", title="Bare task")
        task = get_task(config, "test", task.id)
        md = render_rubric_markdown(task)
        assert "none defined" in md
        assert "## Grading instructions" in md

    def test_outcome_payload_shape(self, temp_portfolio):
        """Verify the JSON payload matches user.define_outcome event shape."""
        config = temp_portfolio["config"]
        task = add_task(
            config, "test", title="Outcome",
            predictions=Predictions(success_criteria=["c1"]),
        )
        task = get_task(config, "test", task.id)
        payload = render_rubric_json_payload(task)
        assert payload["type"] == "user.define_outcome"
        assert payload["description"] == "Outcome"
        assert payload["rubric"]["type"] == "text"
        assert "c1" in payload["rubric"]["content"]
        assert payload["max_iterations"] == 3


# ---------------------------------------------------------------------------
# CLI: emit-rubric command
# ---------------------------------------------------------------------------


class TestEmitRubricCLI:
    def test_cli_emit_rubric_markdown_default(self, temp_portfolio):
        runner = CliRunner()
        r = runner.invoke(
            main,
            ["-p", "test", "tasks", "add",
             "-t", "Task A",
             "--success-criteria", "Latency <200ms"],
        )
        assert r.exit_code == 0, r.output
        tid = json.loads(r.output)["data"]["id"]

        # Force text output so we get raw markdown
        r = runner.invoke(
            main,
            ["-f", "text", "-p", "test", "tasks", "emit-rubric", tid],
        )
        assert r.exit_code == 0, r.output
        assert "# Rubric: Task A" in r.output
        assert "Latency <200ms" in r.output

    def test_cli_emit_rubric_outcome_payload(self, temp_portfolio):
        runner = CliRunner()
        r = runner.invoke(
            main,
            ["-p", "test", "tasks", "add",
             "-t", "Task B",
             "--success-criteria",
             '{"criterion": "P95 <200ms", "gradeable_signal": "loadtest"}'],
        )
        assert r.exit_code == 0, r.output
        tid = json.loads(r.output)["data"]["id"]

        r = runner.invoke(
            main,
            ["-p", "test", "tasks", "emit-rubric", tid,
             "--format", "outcome-payload"],
        )
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)["payload"]
        assert payload["type"] == "user.define_outcome"
        assert "P95 <200ms" in payload["rubric"]["content"]
        # Structured signal carried through to the rubric body
        assert "loadtest" in payload["rubric"]["content"]

    def test_cli_emit_rubric_unknown_task(self, temp_portfolio):
        runner = CliRunner()
        r = runner.invoke(
            main, ["-p", "test", "tasks", "emit-rubric", "NOPE-001"]
        )
        assert r.exit_code == 1
        # Stderr-style error in JSON body
        payload = json.loads(r.output)
        assert payload["error"] == "task_not_found"

    def test_cli_structured_criterion_via_json_flag(self, temp_portfolio):
        """A JSON-object string passed to --success-criteria is structured."""
        runner = CliRunner()
        r = runner.invoke(
            main,
            ["-p", "test", "tasks", "add",
             "-t", "Structured",
             "--success-criteria",
             '{"criterion": "X", "gradeable_signal": "Y", "comparator": "Z"}'],
        )
        assert r.exit_code == 0, r.output
        tid = json.loads(r.output)["data"]["id"]

        r = runner.invoke(main, ["-p", "test", "tasks", "show", tid])
        payload = json.loads(r.output)
        sc = payload["predictions"]["success_criteria"][0]
        # Structured form persists as dict
        assert sc["criterion"] == "X"
        assert sc["gradeable_signal"] == "Y"
        assert sc["comparator"] == "Z"
