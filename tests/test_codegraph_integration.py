"""Tests for CodeGraph integration points (CLAWP-027..031).

Every test stubs the codegraph subprocess so it runs offline. The
clawpm.codegraph module functions are monkeypatched at the import
boundary; the integration code paths exercise full code-paths.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from clawpm import codegraph as cg
from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import Predictions, TaskState
from clawpm.tasks import add_task


@pytest.fixture
def temp_portfolio_with_repo():
    """Portfolio with a project that has a fake repo + .codegraph/ dir."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_cg_test_")
    portfolio_root = Path(temp_dir)
    repo_dir = portfolio_root / "repo"
    repo_dir.mkdir()
    # Git init so dispatch / worktree tests can work
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    (repo_dir / "README.md").write_text("hi", encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a",
         "-C", str(repo_dir), "add", "README.md"], check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a",
         "-C", str(repo_dir), "commit", "-q", "-m", "init"], check=True,
    )

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{portfolio_root.as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n'
    )
    project_meta = repo_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        f'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
        f'repo_path = "{repo_dir.as_posix()}"\n'
    )
    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    config = load_portfolio_config(portfolio_root)
    yield {"root": portfolio_root, "repo_dir": repo_dir, "config": config}
    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    try:
        subprocess.run(["git", "-C", str(repo_dir), "worktree", "prune"],
                       check=False, capture_output=True)
    except Exception:
        pass
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# codegraph.py helpers
# ---------------------------------------------------------------------------


class TestCodegraphAvailability:
    def test_is_codegraph_available_returns_bool(self):
        # Don't assert truth — the test runner may or may not have it.
        # Just verify the function returns a bool without raising.
        assert isinstance(cg.is_codegraph_available(), bool)

    def test_is_project_indexed_false_for_empty_dir(self, tmp_path):
        assert cg.is_project_indexed(tmp_path) is False

    def test_is_project_indexed_true_when_dir_exists(self, tmp_path):
        (tmp_path / ".codegraph").mkdir()
        assert cg.is_project_indexed(tmp_path) is True


class TestPathParser:
    def test_parses_python_paths(self):
        text = "Found in `src/clawpm/agent.py` and `src/clawpm/dispatch.py`."
        globs = cg._parse_file_paths_to_globs(text, Path("."), max_globs=5)
        # Both files in src/clawpm/ — should collapse to dir/** glob
        assert any("src/clawpm" in g for g in globs)

    def test_parses_typescript_paths(self):
        text = "See `src/api/login.ts` and `src/api/logout.ts`."
        globs = cg._parse_file_paths_to_globs(text, Path("."), max_globs=5)
        # Two files same dir → dir/** rollup
        assert any(g == "src/api/**" for g in globs)

    def test_caps_at_max_globs(self):
        text = "\n".join(f"`src/m{i}/file.py`" for i in range(20))
        globs = cg._parse_file_paths_to_globs(text, Path("."), max_globs=3)
        assert len(globs) == 3

    def test_single_file_kept_exact(self):
        text = "`src/auth/login.py`"
        globs = cg._parse_file_paths_to_globs(text, Path("."), max_globs=5)
        assert globs == ["src/auth/login.py"]

    def test_monorepo_path_not_truncated(self):
        """Codex PR#9 round-1 P1: prior regex matched at the first
        `src|lib|...` token, so monorepo paths got truncated. Now
        captures the full relative path."""
        text = "`apps/web/src/main.ts` is the entry point."
        globs = cg._parse_file_paths_to_globs(text, Path("."), max_globs=5)
        # Must keep the apps/web/ prefix, not collapse to src/main.ts
        assert any("apps/web" in g for g in globs)
        # And no glob should START with src/ (which would mean truncation)
        assert not any(g.startswith("src/") for g in globs)

    def test_nested_package_layout(self):
        """packages/foo/lib/bar.py should produce packages/foo/lib
        path anchoring, not lib/bar.py."""
        text = "`packages/foo/lib/bar.py`"
        globs = cg._parse_file_paths_to_globs(text, Path("."), max_globs=5)
        assert globs == ["packages/foo/lib/bar.py"]

    def test_deterministic_order_across_runs(self):
        """Codex PR#9 round-1 P2: set() iteration is hash-randomised,
        so suggested_scope was non-deterministic. Now uses dict.fromkeys
        for ordered dedup."""
        text = (
            "Touched `src/a/x.py`, `src/b/y.py`, `src/c/z.py`, "
            "`src/d/w.py`, `src/e/v.py`, `src/f/u.py`."
        )
        results = [
            cg._parse_file_paths_to_globs(text, Path("."), max_globs=3)
            for _ in range(10)
        ]
        first = results[0]
        for r in results[1:]:
            assert r == first, f"non-deterministic: {first!r} vs {r!r}"

    def test_truncation_drops_consistent_globs(self):
        """When max_globs caps the result, the SAME entries must be
        dropped every run (insertion order)."""
        text = "\n".join(
            f"See `module{i}/file.py`" for i in range(10)
        )
        results = [
            cg._parse_file_paths_to_globs(text, Path("."), max_globs=3)
            for _ in range(5)
        ]
        first = results[0]
        for r in results[1:]:
            assert r == first

    def test_root_file_captured(self):
        """Codex PR#9 round-2 P2: previously {1,6} segment bound dropped
        root files like `main.py`. Now {0,12} allows zero segments."""
        text = "See `main.py` for the entry point."
        globs = cg._parse_file_paths_to_globs(text, Path("."), max_globs=5)
        assert "main.py" in globs

    def test_deep_monorepo_path_captured(self):
        """Upper bound widened to 12 segments accommodates layouts like
        apps/web/packages/foo/src/lib/main.ts (6 dir segments)."""
        text = "Entry: `apps/web/packages/foo/src/lib/main.ts`"
        globs = cg._parse_file_paths_to_globs(text, Path("."), max_globs=5)
        assert any("apps/web/packages/foo/src/lib" in g for g in globs)

    def test_repo_root_files_listed_individually(self):
        """Codex PR#9 round-3 P2: when multiple files are at the repo
        root, parent is empty — naively rolling up produced '/**'
        (catches everything). Fix: list root files individually."""
        text = "Touched `main.py` and `app.py` directly."
        globs = cg._parse_file_paths_to_globs(text, Path("."), max_globs=5)
        # No '/**' (would be the broken behaviour)
        assert "/**" not in globs
        # Both root files should be listed individually
        assert "main.py" in globs
        assert "app.py" in globs

    def test_repo_root_single_file_kept(self):
        text = "See `main.py` only."
        globs = cg._parse_file_paths_to_globs(text, Path("."), max_globs=5)
        assert globs == ["main.py"]


class TestSymbolParser:
    def test_parses_symbol_names(self):
        text = "The `authenticate_user` function calls `verify_token`."
        symbols = cg._parse_symbol_names(text)
        assert "authenticate_user" in symbols
        assert "verify_token" in symbols

    def test_handles_dotted_names(self):
        text = "Calls `auth.middleware.verify` from `cli.commands`."
        symbols = cg._parse_symbol_names(text)
        assert "auth.middleware.verify" in symbols
        assert "cli.commands" in symbols


class TestCountCodeFiles:
    def test_counts_only_code_extensions(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
        (tmp_path / "src" / "b.ts").write_text("x", encoding="utf-8")
        (tmp_path / "README.md").write_text("x", encoding="utf-8")  # not code
        (tmp_path / "config.toml").write_text("x", encoding="utf-8")  # not code
        assert cg.count_code_files(tmp_path) == 2

    def test_skips_node_modules_and_venv(self, tmp_path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "lib.js").write_text("x", encoding="utf-8")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "lib.py").write_text("x", encoding="utf-8")
        (tmp_path / "main.py").write_text("x", encoding="utf-8")
        assert cg.count_code_files(tmp_path) == 1  # only main.py

    def test_max_walk_bounds_scanned_entries_not_matches(self, tmp_path):
        """Codex PR#9 round-4 P2: a data-heavy repo with many non-code
        files used to walk indefinitely because max_walk gated on
        matches. Now caps on scanned entries — the walk terminates
        regardless of code density."""
        # Seed 200 non-code files, only 5 code files
        for i in range(200):
            (tmp_path / f"asset{i}.bin").write_text("x", encoding="utf-8")
        for i in range(5):
            (tmp_path / f"file{i}.py").write_text("x", encoding="utf-8")

        # Cap at 50 entries — must terminate before walking all 205
        count = cg.count_code_files(tmp_path, max_walk=50)
        # We can't know exactly how many code files were seen (depends
        # on os.walk ordering), but the function MUST return without
        # walking past the cap. Count is bounded by 5 (the actual code
        # file population) — the assertion is purely about termination.
        assert count <= 5
        # And the function returned (didn't hang) — the assertion above
        # being reachable proves that.


# ---------------------------------------------------------------------------
# CLAWP-027: --predict-scope auto-population
# ---------------------------------------------------------------------------


class TestScopeAutoPopulate:
    def test_tasks_add_surfaces_suggested_scope_when_codegraph_returns_paths(
        self, temp_portfolio_with_repo, monkeypatch
    ):
        # Stub the codegraph helper to return fake suggestions
        def fake_suggest(text, repo_path, **kwargs):
            return ["src/auth/**", "tests/test_auth.py"]

        monkeypatch.setattr(
            "clawpm.codegraph.suggest_scope_from_text", fake_suggest
        )

        runner = CliRunner()
        r = runner.invoke(main, [
            "-p", "test", "tasks", "add",
            "-t", "refactor auth flow",
            "-b", "tighten the JWT validation path",
        ])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["data"]["suggested_scope"] == [
            "src/auth/**", "tests/test_auth.py",
        ]

    def test_tasks_add_silent_when_predict_scope_already_pinned(
        self, temp_portfolio_with_repo, monkeypatch
    ):
        """If operator already passed --predict-scope, don't surface
        suggestions (they explicitly chose their scope)."""
        called = {"hit": False}

        def fake_suggest(text, repo_path, **kwargs):
            called["hit"] = True
            return ["src/auth/**"]

        monkeypatch.setattr(
            "clawpm.codegraph.suggest_scope_from_text", fake_suggest
        )

        runner = CliRunner()
        r = runner.invoke(main, [
            "-p", "test", "tasks", "add",
            "-t", "X", "--predict-scope", "src/other/**",
        ])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert "suggested_scope" not in payload["data"]
        assert called["hit"] is False

    def test_tasks_add_silent_when_codegraph_unavailable(
        self, temp_portfolio_with_repo, monkeypatch
    ):
        def fake_suggest(text, repo_path, **kwargs):
            return []  # no codegraph index = empty suggestions

        monkeypatch.setattr(
            "clawpm.codegraph.suggest_scope_from_text", fake_suggest
        )

        runner = CliRunner()
        r = runner.invoke(main, ["-p", "test", "tasks", "add", "-t", "X"])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        assert "suggested_scope" not in payload["data"]


# ---------------------------------------------------------------------------
# CLAWP-029: agent dispatch initialises codegraph in worktree
# ---------------------------------------------------------------------------


class TestAgentDispatchCodegraphInit:
    def test_dispatch_calls_init_in_worktree_by_default(
        self, temp_portfolio_with_repo, monkeypatch
    ):
        from clawpm import agent as ag

        called = {"hit": False, "path": None}

        def fake_init(path, timeout=60):
            called["hit"] = True
            called["path"] = path
            return True

        # Monkeypatch the codegraph.init_in_worktree symbol used by agent.py
        monkeypatch.setattr("clawpm.codegraph.init_in_worktree", fake_init)

        def fake_invoker(prompt):
            return '{"ok": true, "reason": "stub"}'

        result = ag.dispatch_agent(
            config=temp_portfolio_with_repo["config"],
            project_id="test",
            prompt="do thing",
            success_criteria=["X"],
            judge_invoker=fake_invoker,
        )
        assert called["hit"] is True
        assert result["codegraph_initialized"] is True

    def test_dispatch_skips_codegraph_when_init_codegraph_false(
        self, temp_portfolio_with_repo, monkeypatch
    ):
        from clawpm import agent as ag

        called = {"hit": False}

        def fake_init(path, timeout=60):
            called["hit"] = True
            return True

        monkeypatch.setattr("clawpm.codegraph.init_in_worktree", fake_init)

        def fake_invoker(prompt):
            return '{"ok": true, "reason": "stub"}'

        result = ag.dispatch_agent(
            config=temp_portfolio_with_repo["config"],
            project_id="test",
            prompt="x",
            success_criteria=["X"],
            judge_invoker=fake_invoker,
            init_codegraph=False,
        )
        assert called["hit"] is False
        assert result["codegraph_initialized"] is False


# ---------------------------------------------------------------------------
# CLAWP-028: resume enriched with codegraph_context
# ---------------------------------------------------------------------------


class TestResumeCodegraphEnrichment:
    def test_resume_includes_codegraph_context_when_indexed(
        self, temp_portfolio_with_repo, monkeypatch
    ):
        config = temp_portfolio_with_repo["config"]
        # Seed an in-progress task
        task = add_task(config, "test", title="auth middleware refactor")
        from clawpm.tasks import change_task_state
        change_task_state(config, "test", task.id, TaskState.PROGRESS)

        # Stub codegraph.context_brief
        def fake_brief(query, repo_path, **kwargs):
            return "## CodeGraph context\n- `authenticate_user` in src/auth/login.py"

        monkeypatch.setattr("clawpm.codegraph.context_brief", fake_brief)

        from clawpm.resume import gather_signals
        sig = gather_signals(config, "test")
        assert "authenticate_user" in sig.codegraph_context

    def test_resume_codegraph_context_empty_when_no_task(
        self, temp_portfolio_with_repo, monkeypatch
    ):
        config = temp_portfolio_with_repo["config"]

        def fake_brief(query, repo_path, **kwargs):
            return "should not be called"

        monkeypatch.setattr("clawpm.codegraph.context_brief", fake_brief)

        from clawpm.resume import gather_signals
        sig = gather_signals(config, "test")
        # No in-progress task → no codegraph context
        assert sig.codegraph_context == ""


# ---------------------------------------------------------------------------
# CLAWP-030: reference-task scoring with codegraph symbols
# ---------------------------------------------------------------------------


class TestReferenceTaskSemanticOverlap:
    def test_similarity_score_with_codegraph_symbols(self):
        from clawpm.reflect import _similarity_score
        # Without codegraph axis (legacy callers): score unchanged
        score_legacy = _similarity_score(
            predictions={"complexity": "m"},
            target_complexity="m",
            target_scope=[],
            target_frameworks=set(),
            target_sc_tokens=set(),
        )
        # With matching symbols
        score_with = _similarity_score(
            predictions={"complexity": "m"},
            target_complexity="m",
            target_scope=[],
            target_frameworks=set(),
            target_sc_tokens=set(),
            target_codegraph_symbols={"a", "b", "c"},
            candidate_codegraph_symbols={"a", "b"},
        )
        assert score_with > score_legacy
        assert score_with - score_legacy == 2  # +1 per shared, 2 shared

    def test_codegraph_axis_capped_at_4(self):
        from clawpm.reflect import _similarity_score
        score = _similarity_score(
            predictions={},
            target_complexity=None,
            target_scope=[],
            target_frameworks=set(),
            target_sc_tokens=set(),
            target_codegraph_symbols={f"s{i}" for i in range(20)},
            candidate_codegraph_symbols={f"s{i}" for i in range(20)},
        )
        # 20 shared, cap at +4 → 4 + baseline 1 = 5
        assert score == 5

    def test_no_codegraph_when_repo_path_none(self, temp_portfolio_with_repo):
        """find_reference_tasks without repo_path = pre-CLAWP-030 behaviour."""
        from clawpm.reflect import find_reference_tasks
        root = temp_portfolio_with_repo["root"]
        # Seed a reflection
        ref_file = root / "reflections" / "TEST-001.jsonl"
        ref_file.parent.mkdir(exist_ok=True)
        ref_file.write_text(json.dumps({
            "event": "task_done",
            "task_id": "TEST-001",
            "project_id": "test",
            "predictions": {"complexity": "m", "duration_min": 60},
            "actuals": {"duration_min": 60, "complexity": "m"},
            "deltas": {"duration_ratio": 1.0},
        }) + "\n", encoding="utf-8")

        results = find_reference_tasks(
            root, project_id="test", complexity="m"
        )  # No repo_path = no codegraph axis = no error
        assert len(results) == 1


# ---------------------------------------------------------------------------
# CLAWP-031: doctor advisory
# ---------------------------------------------------------------------------


class TestDoctorCodegraphAdvisory:
    def test_advisory_for_code_bearing_project_without_index(
        self, temp_portfolio_with_repo
    ):
        # Seed 60 .py files (above the 50 threshold)
        src = temp_portfolio_with_repo["repo_dir"] / "src"
        src.mkdir()
        for i in range(60):
            (src / f"file{i}.py").write_text("x", encoding="utf-8")

        runner = CliRunner()
        r = runner.invoke(main, ["doctor"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert "codegraph_advice" in payload
        advices = payload["codegraph_advice"]
        assert any(a["project_id"] == "test" for a in advices)

    def test_no_advisory_when_indexed(self, temp_portfolio_with_repo):
        # Seed code files AND .codegraph/
        src = temp_portfolio_with_repo["repo_dir"] / "src"
        src.mkdir()
        for i in range(60):
            (src / f"file{i}.py").write_text("x", encoding="utf-8")
        (temp_portfolio_with_repo["repo_dir"] / ".codegraph").mkdir()

        runner = CliRunner()
        r = runner.invoke(main, ["doctor"])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        advices = payload.get("codegraph_advice", [])
        assert not any(a["project_id"] == "test" for a in advices)

    def test_no_advisory_below_threshold(self, temp_portfolio_with_repo):
        src = temp_portfolio_with_repo["repo_dir"] / "src"
        src.mkdir()
        for i in range(10):  # below 50 threshold
            (src / f"file{i}.py").write_text("x", encoding="utf-8")

        runner = CliRunner()
        r = runner.invoke(main, ["doctor"])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        advices = payload.get("codegraph_advice", [])
        assert not any(a["project_id"] == "test" for a in advices)

    def test_text_mode_surfaces_advisory_when_only_signal(
        self, temp_portfolio_with_repo
    ):
        """Codex PR#9 round-3 P2: text-mode `[OK] No issues found` guard
        must consider codegraph_advice. Otherwise operators with only
        an advisory never see it on the default text output."""
        src = temp_portfolio_with_repo["repo_dir"] / "src"
        src.mkdir()
        for i in range(60):  # above threshold
            (src / f"file{i}.py").write_text("x", encoding="utf-8")

        runner = CliRunner()
        r = runner.invoke(main, ["-f", "text", "doctor"])
        assert r.exit_code == 0, r.output
        # Must NOT see the all-clear message when an advisory exists
        assert "No issues found" not in r.output
        # Must see the ADVICE line
        assert "[ADVICE]" in r.output
        assert "codegraph" in r.output
