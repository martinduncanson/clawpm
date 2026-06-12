"""CLAWP-060: Windows argv glob-expansion fix for scope options.

The installed clawpm.exe on Windows uses the MSVC CRT argv processor, which
glob-expands wildcard tokens (e.g. ``src/**``) BEFORE Python's ``main()`` sees
them.  The durable, build-agnostic fix is a file-input path:
``--scope-file``, ``--predict-scope-file``, ``--out-of-scope-file``.

Patterns are read from the file ONE PER LINE and stored LITERALLY -- they
never become shell/CRT argv tokens, so the expansion cannot happen.

STEP-1 MECHANISM FINDING (confirmed on Windows with installed clawpm.exe):
  The CRT/launcher glob-expands argv tokens BEFORE Python main() sees them.
  Proof: ``clawpm tasks add --title X --scope 'src/**'`` (run from
  F:/git/clawpm/) failed with:
    "Got unexpected extra arguments (src\\clawpm src\\clawpm\\cli.py ...)"
  The ``src/**`` pattern was expanded into the actual filesystem entries and
  received by Click as separate positional tokens.  Passing a non-matching
  pattern (``zzz_nomatch_xyzzy_**``) passes through intact.
  This confirms CRT/launcher globbing -- not a shell issue.

NOTE: emit-tree JSON via stdin is already immune (the JSON blob is a single
quoted argument, not a glob-valued token).
"""

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
from clawpm.tasks import add_task, get_task


# ---------------------------------------------------------------------------
# Shared fixture (mirrors test_scope.py structure)
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    """Temporary portfolio with a single test project."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_clawp060_test_")
    portfolio_root = Path(temp_dir)

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )

    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()

    project_dir = projects_dir / "test-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        'id = "test"\nname = "Test Project"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )

    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)

    config = load_portfolio_config(portfolio_root)

    yield {
        "root": portfolio_root,
        "project_dir": project_dir,
        "tasks_dir": tasks_dir,
        "config": config,
    }

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir)


def _write_patterns_file(tmp_dir: Path, filename: str, patterns: list[str]) -> Path:
    """Write one-pattern-per-line file; return its path."""
    p = tmp_dir / filename
    p.write_text("\n".join(patterns) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. tasks add --scope-file stores patterns LITERALLY
# ---------------------------------------------------------------------------


class TestScopeFile:
    def test_scope_file_stored_literally_tasks_add(self, temp_portfolio):
        """--scope-file patterns are stored verbatim (no CRT expansion)."""
        tmp = temp_portfolio["root"]
        patterns = ["a/**", "b/c/**", "tests/**/*.py"]
        scope_file = _write_patterns_file(tmp, "scope.txt", patterns)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Scope file test",
                "--scope-file", str(scope_file),
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["scope"] == patterns, (
            f"Expected {patterns}, got {data['data']['scope']}"
        )

    def test_scope_file_persisted_to_disk(self, temp_portfolio):
        """Patterns read via --scope-file survive a disk round-trip."""
        config = temp_portfolio["config"]
        tmp = temp_portfolio["root"]
        patterns = ["src/**", "tests/**"]
        scope_file = _write_patterns_file(tmp, "scope2.txt", patterns)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Roundtrip test",
                "--scope-file", str(scope_file),
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        task_id = json.loads(result.output)["data"]["id"]

        reloaded = get_task(config, "test", task_id)
        assert reloaded is not None
        assert reloaded.scope == patterns

    def test_scope_file_ignores_blank_lines_and_comments(self, temp_portfolio):
        """Blank lines and # comments in scope-file are skipped."""
        tmp = temp_portfolio["root"]
        scope_file = tmp / "scope_comments.txt"
        scope_file.write_text(
            "# this is a comment\n"
            "\n"
            "src/**\n"
            "  \n"
            "tests/**\n",
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Comment skip test",
                "--scope-file", str(scope_file),
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["scope"] == ["src/**", "tests/**"]

    def test_scope_and_scope_file_are_combined(self, temp_portfolio):
        """--scope and --scope-file can be combined; file patterns appended."""
        tmp = temp_portfolio["root"]
        scope_file = _write_patterns_file(tmp, "scope_extra.txt", ["extra/**"])

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Combined scope test",
                "--scope", "inline/**",
                "--scope-file", str(scope_file),
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "inline/**" in data["data"]["scope"]
        assert "extra/**" in data["data"]["scope"]


# ---------------------------------------------------------------------------
# 2. tasks edit --scope-file
# ---------------------------------------------------------------------------


class TestScopeFileEdit:
    def test_edit_scope_file_replaces_scope(self, temp_portfolio):
        """tasks edit --scope-file replaces the task's scope."""
        config = temp_portfolio["config"]
        tmp = temp_portfolio["root"]

        task = add_task(config, "test", "Edit scope file task", scope=["old/**"])
        assert task is not None

        patterns = ["new/**", "also/**"]
        scope_file = _write_patterns_file(tmp, "new_scope.txt", patterns)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "edit", task.id,
                "--scope-file", str(scope_file),
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["scope"] == patterns

        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert reloaded.scope == patterns


# ---------------------------------------------------------------------------
# 3. --predict-scope-file (tasks add)
# ---------------------------------------------------------------------------


class TestPredictScopeFile:
    def test_predict_scope_file_stored_literally(self, temp_portfolio):
        """--predict-scope-file stores patterns literally in predictions."""
        tmp = temp_portfolio["root"]
        patterns = ["a/**", "b/**/*.ts"]
        pscope_file = _write_patterns_file(tmp, "pscope.txt", patterns)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Predict scope file test",
                "--predict-scope-file", str(pscope_file),
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        preds = data["data"].get("predictions", {})
        assert preds.get("files_scope") == patterns, (
            f"Expected {patterns}, got {preds.get('files_scope')}"
        )

    def test_predict_scope_and_file_combined(self, temp_portfolio):
        """--predict-scope and --predict-scope-file can coexist."""
        tmp = temp_portfolio["root"]
        pscope_file = _write_patterns_file(tmp, "pscope2.txt", ["file/**"])

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Predict scope combined",
                "--predict-scope", "inline/**",
                "--predict-scope-file", str(pscope_file),
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        preds = data["data"].get("predictions", {})
        files_scope = preds.get("files_scope", [])
        assert "inline/**" in files_scope
        assert "file/**" in files_scope


# ---------------------------------------------------------------------------
# 4. --out-of-scope-file (tasks add)
# ---------------------------------------------------------------------------


class TestOutOfScopeFile:
    def test_out_of_scope_file_stored_literally(self, temp_portfolio):
        """--out-of-scope-file stores patterns literally."""
        tmp = temp_portfolio["root"]
        patterns = ["docs/**", "*.md"]
        oos_file = _write_patterns_file(tmp, "oos.txt", patterns)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Out of scope file test",
                "--out-of-scope-file", str(oos_file),
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"].get("out_of_scope") == patterns, (
            f"Expected {patterns}, got {data['data'].get('out_of_scope')}"
        )

    def test_out_of_scope_and_file_combined(self, temp_portfolio):
        """--out-of-scope and --out-of-scope-file can coexist."""
        tmp = temp_portfolio["root"]
        oos_file = _write_patterns_file(tmp, "oos2.txt", ["extra/**"])

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Out of scope combined",
                "--out-of-scope", "inline/**",
                "--out-of-scope-file", str(oos_file),
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        oos = data["data"].get("out_of_scope", [])
        assert "inline/**" in oos
        assert "extra/**" in oos


# ---------------------------------------------------------------------------
# 5. Error handling: non-existent scope-file
# ---------------------------------------------------------------------------


class TestScopeFileErrors:
    def test_missing_scope_file_exits_with_error(self, temp_portfolio):
        """A non-existent --scope-file path causes a non-zero exit."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Missing file test",
                "--scope-file", "/no/such/file.txt",
                "--project", "test",
            ],
        )
        assert result.exit_code != 0

    def test_missing_predict_scope_file_exits_with_error(self, temp_portfolio):
        """A non-existent --predict-scope-file path causes a non-zero exit."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Missing pscope file",
                "--predict-scope-file", "/no/such/pscope.txt",
                "--project", "test",
            ],
        )
        assert result.exit_code != 0

    def test_missing_out_of_scope_file_exits_with_error(self, temp_portfolio):
        """A non-existent --out-of-scope-file path causes a non-zero exit."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Missing oos file",
                "--out-of-scope-file", "/no/such/oos.txt",
                "--project", "test",
            ],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# 6. tasks edit --predict-scope-file, --out-of-scope-file
# ---------------------------------------------------------------------------


class TestEditFileOptions:
    def test_edit_predict_scope_file(self, temp_portfolio):
        """tasks edit --predict-scope-file updates predictions.files_scope."""
        config = temp_portfolio["config"]
        tmp = temp_portfolio["root"]

        task = add_task(config, "test", "Edit predict scope")
        assert task is not None

        patterns = ["src/**", "tests/**"]
        pscope_file = _write_patterns_file(tmp, "edit_pscope.txt", patterns)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "edit", task.id,
                "--predict-scope-file", str(pscope_file),
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        preds = data["data"].get("predictions", {})
        assert preds.get("files_scope") == patterns

    def test_edit_out_of_scope_file(self, temp_portfolio):
        """tasks edit --out-of-scope-file updates out_of_scope."""
        config = temp_portfolio["config"]
        tmp = temp_portfolio["root"]

        task = add_task(config, "test", "Edit oos file")
        assert task is not None

        patterns = ["vendor/**", "*.lock"]
        oos_file = _write_patterns_file(tmp, "edit_oos.txt", patterns)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "edit", task.id,
                "--out-of-scope-file", str(oos_file),
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"].get("out_of_scope") == patterns
