"""Tests for reference-task surfacing (CLAWP-023)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import TaskComplexity
from clawpm.reflect import (
    _tokenise_criteria,
    _similarity_score,
    _scope_overlap_simple,
    find_reference_tasks,
)


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_refsugg_test_")
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
    (portfolio_root / "reflections").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    config = load_portfolio_config(portfolio_root)
    yield {
        "root": portfolio_root,
        "config": config,
    }
    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


def _seed_reflection(
    root: Path,
    task_id: str,
    project_id: str = "test",
    complexity: str = "m",
    files_scope: list[str] | None = None,
    frameworks: list[str] | None = None,
    success_criteria: list[str] | None = None,
    duration_min_predicted: int = 60,
    duration_min_actual: int = 60,
    iterations: int | None = None,
    duration_ratio: float | None = None,
) -> None:
    """Append a task_done event to a reflection JSONL for the test corpus."""
    rec = {
        "event": "task_done",
        "task_id": task_id,
        "project_id": project_id,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "predictions": {
            "duration_min": duration_min_predicted,
            "complexity": complexity,
            "files_scope": files_scope or [],
            "frameworks": frameworks or [],
            "success_criteria": success_criteria or [],
            "predicted_iterations": None,
        },
        "actuals": {
            "duration_min": duration_min_actual,
            "complexity": complexity,
            "files_changed": None,
            "files_touched": [],
            "iterations": iterations,
        },
        "deltas": {
            "duration_ratio": duration_ratio or round(duration_min_actual / duration_min_predicted, 4),
            "complexity_match": True,
        },
        "process_lesson": None,
        "surprise_taxonomy": [],
    }
    ref_file = root / "reflections" / f"{task_id}.jsonl"
    with open(ref_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestTokeniseCriteria:
    def test_strings_lowercased(self):
        toks = _tokenise_criteria(["P95 LATENCY <200ms"])
        assert "p95" in toks
        assert "latency" in toks
        # `<` is stripped as punctuation; the value-bearing token is "200ms"
        assert "200ms" in toks

    def test_stopwords_dropped(self):
        toks = _tokenise_criteria(["the test is in the suite"])
        assert "test" in toks
        assert "suite" in toks
        assert "the" not in toks
        assert "is" not in toks

    def test_structured_dict_form_accepted(self):
        toks = _tokenise_criteria([
            {"criterion": "tests pass", "gradeable_signal": "pytest"}
        ])
        assert "tests" in toks
        assert "pass" in toks
        # gradeable_signal is NOT tokenised (it's the evidence side, not the contract)
        assert "pytest" not in toks


class TestScopeOverlapSimple:
    def test_exact_match(self):
        assert _scope_overlap_simple("src/auth/**", "src/auth/**")

    def test_subtree_overlap(self):
        assert _scope_overlap_simple("src/auth/**", "src/auth/handlers/**")
        assert _scope_overlap_simple("src/auth/login.py", "src/auth/**")

    def test_disjoint(self):
        assert not _scope_overlap_simple("src/auth/**", "src/billing/**")


class TestSimilarityScore:
    def test_complexity_match_scores_3(self):
        s = _similarity_score(
            predictions={"complexity": "m"},
            target_complexity="m",
            target_scope=[],
            target_frameworks=set(),
            target_sc_tokens=set(),
        )
        # 3 (complexity) + 1 (baseline) = 4
        assert s == 4

    def test_complexity_mismatch_no_bonus(self):
        s = _similarity_score(
            predictions={"complexity": "s"},
            target_complexity="m",
            target_scope=[],
            target_frameworks=set(),
            target_sc_tokens=set(),
        )
        assert s == 1  # baseline only

    def test_scope_overlap_scores(self):
        s = _similarity_score(
            predictions={"files_scope": ["src/auth/**"]},
            target_complexity=None,
            target_scope=["src/auth/handlers/**"],
            target_frameworks=set(),
            target_sc_tokens=set(),
        )
        # 2 (scope) + 1 (baseline) = 3
        assert s == 3

    def test_framework_intersection(self):
        s = _similarity_score(
            predictions={"frameworks": ["click", "pytest"]},
            target_complexity=None,
            target_scope=[],
            target_frameworks={"click"},
            target_sc_tokens=set(),
        )
        # 2 (one framework match) + 1 = 3
        assert s == 3

    def test_sc_tokens_capped_at_4(self):
        # 30 matching tokens / 3 = 10 → capped to 4
        common = {f"tok{i}" for i in range(30)}
        s = _similarity_score(
            predictions={
                "success_criteria": [{"criterion": " ".join(common)}],
            },
            target_complexity=None,
            target_scope=[],
            target_frameworks=set(),
            target_sc_tokens=common,
        )
        # 4 (sc cap) + 1 = 5
        assert s == 5


# ---------------------------------------------------------------------------
# find_reference_tasks integration
# ---------------------------------------------------------------------------


class TestFindReferenceTasks:
    def test_empty_corpus_returns_empty(self, temp_portfolio):
        results = find_reference_tasks(
            temp_portfolio["root"], project_id="test", complexity="m"
        )
        assert results == []

    def test_returns_top_k_by_score(self, temp_portfolio):
        root = temp_portfolio["root"]
        # Seed 4 reflections at varying similarity to the target
        _seed_reflection(root, "TEST-001", complexity="m", files_scope=["src/auth/**"], frameworks=["click"])
        _seed_reflection(root, "TEST-002", complexity="s", files_scope=["src/billing/**"])
        _seed_reflection(root, "TEST-003", complexity="m", files_scope=["src/auth/handlers/**"], frameworks=["click", "pytest"])
        _seed_reflection(root, "TEST-004", complexity="l", files_scope=["src/foo/**"])

        results = find_reference_tasks(
            root,
            project_id="test",
            complexity="m",
            files_scope=["src/auth/**"],
            frameworks=["click"],
            k=3,
        )
        ids = [r["task_id"] for r in results]
        # TEST-001 and TEST-003 should rank highest (m + auth scope + click)
        assert "TEST-001" in ids[:2]
        assert "TEST-003" in ids[:2]
        # Scores monotonically non-increasing
        scores = [r["similarity_score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_filters_by_project_id(self, temp_portfolio):
        root = temp_portfolio["root"]
        # Same task_id, different projects — only test should match
        _seed_reflection(root, "SHARED-001", project_id="test", complexity="m")
        _seed_reflection(root, "SHARED-001", project_id="other", complexity="m")

        results = find_reference_tasks(
            root, project_id="test", complexity="m", k=5
        )
        assert len(results) == 1
        # Implementation note: both events go to same file (task_id-keyed),
        # so the "latest task_done in this file matching project" wins.

    def test_voided_events_excluded(self, temp_portfolio):
        """Codex round-1 P2: voided reflection events are bad calibration
        data and MUST NOT be surfaced as reference anchors."""
        root = temp_portfolio["root"]
        # Seed a task_done event, then a void for it
        _seed_reflection(root, "TEST-VOID-001", complexity="m")
        ref_file = root / "reflections" / "TEST-VOID-001.jsonl"
        with open(ref_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "event": "void",
                "task_id": "TEST-VOID-001",
                "project_id": "test",
                "reason": "bad actuals",
                "voided_at": "2026-01-01T00:00:00Z",
            }) + "\n")

        # Seed a non-voided neighbour so the corpus isn't empty
        _seed_reflection(root, "TEST-OK-002", complexity="m")

        results = find_reference_tasks(root, project_id="test", complexity="m")
        ids = {r["task_id"] for r in results}
        assert "TEST-VOID-001" not in ids
        assert "TEST-OK-002" in ids

    def test_legacy_unscoped_void_excludes_too(self, temp_portfolio):
        """Legacy voids without project_id match ANY project (back-compat)."""
        root = temp_portfolio["root"]
        _seed_reflection(root, "TEST-LEGACY-001", complexity="m")
        ref_file = root / "reflections" / "TEST-LEGACY-001.jsonl"
        with open(ref_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "event": "void",
                "task_id": "TEST-LEGACY-001",
                "reason": "pre-stamping era",
                "voided_at": "2025-01-01T00:00:00Z",
            }) + "\n")  # no project_id

        results = find_reference_tasks(root, project_id="test", complexity="m")
        assert all(r["task_id"] != "TEST-LEGACY-001" for r in results)

    def test_skips_events_with_no_actuals(self, temp_portfolio):
        root = temp_portfolio["root"]
        # Write a reflection with null actuals.duration_min
        rec = {
            "event": "task_done",
            "task_id": "EMPTY-001",
            "project_id": "test",
            "predictions": {"complexity": "m", "duration_min": 60},
            "actuals": {"duration_min": None, "complexity": "m"},
            "deltas": {},
        }
        (root / "reflections" / "EMPTY-001.jsonl").write_text(
            json.dumps(rec) + "\n", encoding="utf-8"
        )
        results = find_reference_tasks(root, project_id="test", complexity="m")
        assert results == []

    def test_returns_actuals_alongside_predictions(self, temp_portfolio):
        root = temp_portfolio["root"]
        _seed_reflection(
            root, "TEST-001", complexity="m",
            duration_min_predicted=60, duration_min_actual=180,
            duration_ratio=3.0,
        )
        results = find_reference_tasks(root, project_id="test", complexity="m")
        assert len(results) == 1
        r = results[0]
        assert r["predicted_duration_min"] == 60
        assert r["actual_duration_min"] == 180
        assert r["duration_ratio"] == 3.0

    def test_k_parameter_caps_results(self, temp_portfolio):
        root = temp_portfolio["root"]
        for i in range(10):
            _seed_reflection(root, f"TEST-{i:03d}", complexity="m")
        results = find_reference_tasks(root, project_id="test", complexity="m", k=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# CLI integration: tasks add surfaces suggestions
# ---------------------------------------------------------------------------


class TestCLISurfacesSuggestions:
    def test_tasks_add_includes_suggested_references(self, temp_portfolio):
        root = temp_portfolio["root"]
        # Seed a similar prior task
        _seed_reflection(
            root, "TEST-PRIOR-001",
            complexity="m",
            files_scope=["src/auth/**"],
            duration_min_predicted=60,
            duration_min_actual=180,
            duration_ratio=3.0,
        )

        r = CliRunner().invoke(main, [
            "-p", "test", "tasks", "add",
            "-t", "New auth task",
            "--predict-complexity", "m",
            "--predict-scope", "src/auth/handlers/**",
        ])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert "suggested_references" in payload["data"]
        refs = payload["data"]["suggested_references"]
        assert any(ref["task_id"] == "TEST-PRIOR-001" for ref in refs)

    def test_tasks_add_with_explicit_reference_skips_suggestions(self, temp_portfolio):
        """If operator already pinned --reference-task, don't surface suggestions."""
        root = temp_portfolio["root"]
        _seed_reflection(root, "TEST-PRIOR-001", complexity="m")

        r = CliRunner().invoke(main, [
            "-p", "test", "tasks", "add",
            "-t", "X",
            "--predict-complexity", "m",
            "--reference-task", "TEST-PRIOR-999",  # explicit reference
        ])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        # No suggested_references key when reference already pinned
        assert "suggested_references" not in payload["data"]

    def test_tasks_add_without_predictions_no_suggestions(self, temp_portfolio):
        """No predictions = nothing to anchor against = no suggestions."""
        _seed_reflection(temp_portfolio["root"], "TEST-PRIOR-001", complexity="m")
        r = CliRunner().invoke(main, [
            "-p", "test", "tasks", "add", "-t", "Bare task",
        ])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        assert "suggested_references" not in payload["data"]


# ---------------------------------------------------------------------------
# Performance sanity
# ---------------------------------------------------------------------------


class TestPerformance:
    def test_completes_on_200_event_corpus(self, temp_portfolio):
        """Sanity-cap: 200 reflections should finish well under 5s on
        Windows file IO. Optimisation (e.g. consolidated index file) is
        a future task if the corpus grows past several hundred events."""
        root = temp_portfolio["root"]
        for i in range(200):
            _seed_reflection(root, f"TEST-{i:04d}", complexity="m")
        t0 = time.perf_counter()
        find_reference_tasks(root, project_id="test", complexity="m", k=3)
        elapsed = time.perf_counter() - t0
        assert elapsed < 5.0, f"Took {elapsed:.3f}s on 200-event corpus"
