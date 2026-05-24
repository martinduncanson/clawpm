"""Tests for `clawpm resume` — 2-paragraph session briefing (CLAWP-025).

Coverage:
  (a) Happy path — injected fake judge returns a known 2-paragraph string.
  (b) Cache hit — second call within TTL returns the same string.
  (c) Cache bust — --no-cache regenerates even within TTL.
  (d) Judge unavailable — FileNotFoundError → graceful degraded summary.
  (e) JSON output mode — envelope with status/project_id/briefing.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm import resume as resume_mod
from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.tasks import add_task, change_task_state


CANNED_BRIEFING = (
    "You are on branch feat/foo working on TEST-001 — the test task. The "
    "last commit reworked the resume signals gather.\n"
    "\n"
    "Next, you should land the cache layer and verify the degraded path. "
    "No surprises in the recent reflection events."
)


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_resume_test_")
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
    yield {"root": portfolio_root, "tasks_dir": tasks_dir, "config": config}
    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


def _seed_in_progress(config) -> str:
    """Add a task and move it to PROGRESS — returns task_id."""
    task = add_task(config, "test", title="Test task")
    change_task_state(config, "test", task.id, "progress")
    return task.id


# ---------------------------------------------------------------------------
# (a) Happy path
# ---------------------------------------------------------------------------


class TestResumeHappyPath:
    def test_returns_briefing_from_fake_judge(self, temp_portfolio, monkeypatch):
        config = temp_portfolio["config"]
        _seed_in_progress(config)

        def fake_invoker(prompt: str) -> str:
            # Sanity: the prompt must contain the signals JSON section so the
            # judge has something to work with.
            assert "SIGNALS" in prompt
            assert "Test task" in prompt
            return CANNED_BRIEFING

        monkeypatch.setattr(resume_mod, "_default_resume_invoker", fake_invoker)

        briefing, status = resume_mod.render_briefing(config, "test")
        assert status == "ok"
        assert briefing == CANNED_BRIEFING
        # And it should be written to the cache file
        cache_path = (
            temp_portfolio["root"] / "resume_cache_test.txt"
        )
        assert cache_path.exists()
        assert cache_path.read_text(encoding="utf-8") == CANNED_BRIEFING


# ---------------------------------------------------------------------------
# (b) Cache hit
# ---------------------------------------------------------------------------


class TestResumeCacheHit:
    def test_second_call_within_ttl_returns_cached(
        self, temp_portfolio, monkeypatch
    ):
        config = temp_portfolio["config"]
        _seed_in_progress(config)

        call_count = {"n": 0}

        def fake_invoker(prompt: str) -> str:
            call_count["n"] += 1
            return CANNED_BRIEFING

        monkeypatch.setattr(resume_mod, "_default_resume_invoker", fake_invoker)

        first, status1 = resume_mod.render_briefing(config, "test")
        second, status2 = resume_mod.render_briefing(config, "test")

        assert status1 == "ok"
        assert status2 == "cached"
        assert first == second
        # Invoker called exactly once — second time was a cache hit
        assert call_count["n"] == 1

    def test_expired_cache_is_ignored(self, temp_portfolio, monkeypatch):
        config = temp_portfolio["config"]
        _seed_in_progress(config)

        # Pre-write a stale cache file (mtime 5 minutes ago)
        cache_path = temp_portfolio["root"] / "resume_cache_test.txt"
        cache_path.write_text("STALE", encoding="utf-8")
        stale_time = time.time() - 5 * 60
        os.utime(cache_path, (stale_time, stale_time))

        def fake_invoker(prompt: str) -> str:
            return CANNED_BRIEFING

        monkeypatch.setattr(resume_mod, "_default_resume_invoker", fake_invoker)

        briefing, status = resume_mod.render_briefing(config, "test")
        assert status == "ok"
        assert briefing == CANNED_BRIEFING


# ---------------------------------------------------------------------------
# (c) Cache bust
# ---------------------------------------------------------------------------


class TestResumeNoCacheFlag:
    def test_no_cache_regenerates_within_ttl(self, temp_portfolio, monkeypatch):
        config = temp_portfolio["config"]
        _seed_in_progress(config)

        outputs = iter(["BRIEFING-A", "BRIEFING-B"])

        def fake_invoker(prompt: str) -> str:
            return next(outputs)

        monkeypatch.setattr(resume_mod, "_default_resume_invoker", fake_invoker)

        first, status1 = resume_mod.render_briefing(config, "test")
        second, status2 = resume_mod.render_briefing(
            config, "test", use_cache=False
        )

        assert status1 == "ok"
        assert status2 == "ok"  # NOT "cached" — bypassed
        assert first == "BRIEFING-A"
        assert second == "BRIEFING-B"


# ---------------------------------------------------------------------------
# (d) Judge unavailable → graceful degrade
# ---------------------------------------------------------------------------


class TestResumeDegraded:
    def test_filenotfound_falls_back_to_signals_summary(
        self, temp_portfolio, monkeypatch
    ):
        config = temp_portfolio["config"]
        task_id = _seed_in_progress(config)

        def missing_invoker(prompt: str) -> str:
            raise FileNotFoundError("claude not on PATH")

        monkeypatch.setattr(
            resume_mod, "_default_resume_invoker", missing_invoker
        )

        briefing, status = resume_mod.render_briefing(config, "test")
        assert status == "degraded"
        # The structured summary must mention the in-progress task ID
        assert task_id in briefing
        assert "Test task" in briefing
        # And it must NOT have been cached
        cache_path = temp_portfolio["root"] / "resume_cache_test.txt"
        assert not cache_path.exists()

    def test_runtime_error_with_not_found_message_also_degrades(
        self, temp_portfolio, monkeypatch
    ):
        config = temp_portfolio["config"]
        _seed_in_progress(config)

        def raise_runtime(prompt: str) -> str:
            raise RuntimeError(
                "Judge command not found: 'nonexistent'"
            )

        monkeypatch.setattr(
            resume_mod, "_default_resume_invoker", raise_runtime
        )

        briefing, status = resume_mod.render_briefing(config, "test")
        assert status == "degraded"
        assert "Project: Test" in briefing


# ---------------------------------------------------------------------------
# (e) CLI / JSON envelope
# ---------------------------------------------------------------------------


class TestResumeCLI:
    def test_json_output_mode(self, temp_portfolio, monkeypatch):
        config = temp_portfolio["config"]
        _seed_in_progress(config)

        def fake_invoker(prompt: str) -> str:
            return CANNED_BRIEFING

        monkeypatch.setattr(resume_mod, "_default_resume_invoker", fake_invoker)

        r = CliRunner().invoke(main, ["-f", "json", "-p", "test", "resume"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["status"] == "ok"
        assert payload["project_id"] == "test"
        assert payload["briefing"] == CANNED_BRIEFING
        assert "warning" not in payload

    def test_json_degraded_includes_warning(self, temp_portfolio, monkeypatch):
        config = temp_portfolio["config"]
        _seed_in_progress(config)

        def missing(prompt: str) -> str:
            raise FileNotFoundError("no claude here")

        monkeypatch.setattr(resume_mod, "_default_resume_invoker", missing)

        r = CliRunner().invoke(main, ["-f", "json", "-p", "test", "resume"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["status"] == "degraded"
        assert "warning" in payload
        assert "Project: Test" in payload["briefing"]

    def test_text_output_mode_emits_briefing_to_stdout(
        self, temp_portfolio, monkeypatch
    ):
        config = temp_portfolio["config"]
        _seed_in_progress(config)

        def fake_invoker(prompt: str) -> str:
            return CANNED_BRIEFING

        monkeypatch.setattr(resume_mod, "_default_resume_invoker", fake_invoker)

        r = CliRunner().invoke(main, ["-f", "text", "-p", "test", "resume"])
        assert r.exit_code == 0, r.output
        # Stdout should contain the briefing text
        assert CANNED_BRIEFING.splitlines()[0] in r.output

    def test_no_cache_flag_bypasses_cache(self, temp_portfolio, monkeypatch):
        config = temp_portfolio["config"]
        _seed_in_progress(config)

        # Pre-warm the cache with a sentinel
        (temp_portfolio["root"] / "resume_cache_test.txt").write_text(
            "STALE-SENTINEL", encoding="utf-8"
        )

        def fake_invoker(prompt: str) -> str:
            return CANNED_BRIEFING

        monkeypatch.setattr(resume_mod, "_default_resume_invoker", fake_invoker)

        r = CliRunner().invoke(
            main, ["-f", "json", "-p", "test", "resume", "--no-cache"]
        )
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["status"] == "ok"
        assert payload["briefing"] == CANNED_BRIEFING
        # And the cache was overwritten with fresh content
        assert (
            temp_portfolio["root"] / "resume_cache_test.txt"
        ).read_text(encoding="utf-8") == CANNED_BRIEFING


# ---------------------------------------------------------------------------
# (f) Cross-project isolation (Codex round-7 P2 fix)
# ---------------------------------------------------------------------------


class TestResumeCrossProjectIsolation:
    """The reflection JSONL is keyed by task_id alone, so two projects
    with the same task_id share a file. gather_signals must filter
    events by project_id before tailing, otherwise resume for one
    project surfaces another project's reflections."""

    def test_gather_signals_filters_reflections_by_project(
        self, temp_portfolio
    ):
        config = temp_portfolio["config"]
        task_id = _seed_in_progress(config)

        # Write a reflection file with mixed-project events for the
        # SAME task_id (the actual exploit shape — two projects sharing
        # a task_id share the JSONL file).
        ref_file = (
            temp_portfolio["root"] / "reflections" / f"{task_id}.jsonl"
        )
        ref_file.parent.mkdir(parents=True, exist_ok=True)
        events = [
            {"event": "task_done", "task_id": task_id,
             "project_id": "test", "note": "OUR_PROJECT_NOTE",
             "predictions": {}, "actuals": {}, "deltas": {}},
            {"event": "task_done", "task_id": task_id,
             "project_id": "OTHER_PROJECT", "note": "WRONG_PROJECT_NOTE",
             "predictions": {}, "actuals": {}, "deltas": {}},
            # Legacy unscoped event — must still be included for back-compat
            {"event": "task_done", "task_id": task_id,
             "note": "LEGACY_NOTE",
             "predictions": {}, "actuals": {}, "deltas": {}},
        ]
        with open(ref_file, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        sig = resume_mod.gather_signals(config, "test")
        notes = [r.get("note") for r in sig.recent_reflections]
        assert "OUR_PROJECT_NOTE" in notes
        assert "LEGACY_NOTE" in notes  # back-compat for unscoped events
        assert "WRONG_PROJECT_NOTE" not in notes  # cross-project leak
