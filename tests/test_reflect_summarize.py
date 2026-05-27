"""Tests for the calibration consumers — reflect summarize + suggest (CLAWP-040)."""

from __future__ import annotations

import json

from click.testing import CliRunner

from clawpm.cli import main
from clawpm.models import Actuals, Predictions, TaskComplexity
from clawpm.reflect import (
    summarize_calibration,
    suggest_duration,
    write_reflection_event,
)

from test_agent_dispatch import temp_portfolio_with_repo  # noqa: F401


def _done(root, tid, pred_min, act_min, complexity="m", confidence=3,
          profile=None, project="test"):
    write_reflection_event(
        root,
        event="task_done",
        task_id=tid,
        project_id=project,
        predictions=Predictions(
            duration_min=pred_min,
            complexity=TaskComplexity(complexity) if complexity else None,
            confidence=confidence,
        ),
        actuals=Actuals(
            duration_min=act_min,
            complexity=TaskComplexity(complexity) if complexity else None,
        ),
        agent_profile=profile,
    )


class TestSummarize:
    def test_aggregates_ratio_buckets(self, tmp_path):
        # 3 'm' tasks predicted 600 actual 60 -> ratio 0.1 (10x inflation)
        for i in range(3):
            _done(tmp_path, f"M-{i}", 600, 60, complexity="m", confidence=3)
        # 1 'l' task predicted 100 actual 200 -> ratio 2.0 (optimistic)
        _done(tmp_path, "L-0", 100, 200, complexity="l", confidence=2)
        # 1 dirty row: no actual duration
        _done(tmp_path, "D-0", 600, None, complexity="m")

        summary = summarize_calibration(tmp_path, project_id="test")
        assert summary["total_done"] == 5
        assert summary["with_usable_duration"] == 4
        assert summary["dirty_flagged"] == 1
        assert summary["by_complexity"]["m"]["n"] == 3
        assert summary["by_complexity"]["m"]["median_ratio"] == 0.1
        assert summary["by_complexity"]["l"]["median_ratio"] == 2.0
        assert summary["by_confidence"]["3"]["n"] == 3
        assert "FASTER" in summary["interpretation"]  # overall median < 1

    def test_agent_profile_segmentation(self, tmp_path):
        for i in range(2):
            _done(tmp_path, f"A-{i}", 100, 50, profile="code-architect")
        _done(tmp_path, "G-0", 100, 90, profile=None)
        summary = summarize_calibration(tmp_path, project_id="test")
        assert summary["by_agent_profile"]["code-architect"]["n"] == 2
        assert summary["by_agent_profile"]["unspecified"]["n"] == 1

    def test_project_filter_isolates(self, tmp_path):
        _done(tmp_path, "P1-0", 100, 50, project="proj-a")
        _done(tmp_path, "P2-0", 100, 50, project="proj-b")
        a = summarize_calibration(tmp_path, project_id="proj-a")
        assert a["total_done"] == 1
        allp = summarize_calibration(tmp_path, project_id=None)
        assert allp["total_done"] == 2


class TestSuggest:
    def test_uses_complexity_bucket_when_enough_samples(self, tmp_path):
        for i in range(6):
            _done(tmp_path, f"M-{i}", 600, 60, complexity="m")  # ratio 0.1
        res = suggest_duration(
            tmp_path, complexity="m", predicted_min=600,
            project_id="test", min_bucket=5,
        )
        assert res["bucket"] == "complexity=m"
        assert res["n"] == 6
        assert res["median_ratio"] == 0.1
        assert res["fell_back_to_global"] is False
        assert res["calibrated_duration_min"] == 60  # 600 * 0.1

    def test_falls_back_to_global_when_bucket_thin(self, tmp_path):
        # Only 'm' data; ask for 'l' -> bucket thin -> global fallback.
        for i in range(6):
            _done(tmp_path, f"M-{i}", 600, 60, complexity="m")
        res = suggest_duration(
            tmp_path, complexity="l", predicted_min=600,
            project_id="test", min_bucket=5,
        )
        assert res["fell_back_to_global"] is True
        assert res["bucket"] == "global"
        assert res["calibrated_duration_min"] == 60

    def test_empty_corpus_echoes_estimate(self, tmp_path):
        res = suggest_duration(tmp_path, complexity="m", predicted_min=120,
                               project_id="test")
        assert res["median_ratio"] is None
        assert res["calibrated_duration_min"] == 120  # unchanged, no signal


class TestReflectCLI:
    def _seed(self, config):
        root = config.portfolio_root
        for i in range(6):
            _done(root, f"M-{i}", 600, 60, complexity="m", confidence=3)

    def test_cli_summarize(self, temp_portfolio_with_repo):
        config = temp_portfolio_with_repo["config"]
        self._seed(config)
        r = CliRunner().invoke(main, ["reflect", "summarize", "-p", "test"])
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["data"]["by_complexity"]["m"]["n"] == 6
        assert "FASTER" in out["data"]["interpretation"]

    def test_cli_suggest_complexity(self, temp_portfolio_with_repo):
        config = temp_portfolio_with_repo["config"]
        self._seed(config)
        r = CliRunner().invoke(main, [
            "reflect", "suggest", "-p", "test",
            "--complexity", "m", "--predicted-duration", "10h",
        ])
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        # 10h = 600m, ratio 0.1 -> 60m
        assert out["data"]["calibrated_duration_min"] == 60
        assert out["data"]["bucket"] == "complexity=m"
