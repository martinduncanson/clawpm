"""Tests for dispatch thrashing/runaway detection (CLAWP-062).

Thrashing = N consecutive not-ok, non-impossible iterations with no rubric
progress. When the threshold is reached, the hook surfaces a
stop_condition_tripped verdict so the operator can triage instead of burning
hours on a looping agent.

Coverage:
1. detect_thrashing fires at the configured threshold with all-not-ok iterations.
2. detect_thrashing does NOT fire when any iteration is ok=True (progress).
3. detect_thrashing does NOT fire when any iteration is impossible=True (distinct outcome).
4. detect_thrashing respects a per-task override via Predictions.thrash_threshold.
5. detect_thrashing respects a global env-var default (CLAWPM_THRASH_THRESHOLD).
6. The hook_eval_stop CLI path surfaces a stop_condition_tripped output when thrashing
   is detected (integration: same as 1 but exercised via the CLI runner).
7. A task making progress each iteration (clearing not-ok) is never flagged.
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
from clawpm.models import Predictions
from clawpm.reflect import detect_thrashing, write_iteration_event
from clawpm.tasks import add_task


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_thrash_test_")
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
        'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    config = load_portfolio_config(portfolio_root)
    yield {"root": portfolio_root, "config": config}
    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


def _write_n_not_ok(portfolio_root: Path, task_id: str, n: int) -> None:
    """Write n consecutive not-ok, non-impossible iteration events."""
    for i in range(n):
        write_iteration_event(
            portfolio_root,
            task_id,
            "test",
            verdict_ok=False,
            verdict_reason=f"criterion still unmet (iter {i})",
            verdict_impossible=False,
        )


# ---------------------------------------------------------------------------
# Unit: detect_thrashing
# ---------------------------------------------------------------------------


class TestDetectThrashing:
    def test_fires_at_threshold(self, temp_portfolio):
        """detect_thrashing returns True when last K iterations are all not-ok."""
        root = temp_portfolio["root"]
        task_id = "TEST-001"
        threshold = 3
        _write_n_not_ok(root, task_id, threshold)
        assert detect_thrashing(root, task_id, "test", threshold=threshold) is True

    def test_does_not_fire_below_threshold(self, temp_portfolio):
        """detect_thrashing returns False when fewer than K not-ok iterations exist."""
        root = temp_portfolio["root"]
        task_id = "TEST-002"
        threshold = 3
        _write_n_not_ok(root, task_id, threshold - 1)
        assert detect_thrashing(root, task_id, "test", threshold=threshold) is False

    def test_does_not_fire_when_progress_made(self, temp_portfolio):
        """If any of the last K iterations is ok=True, not thrashing."""
        root = temp_portfolio["root"]
        task_id = "TEST-003"
        threshold = 3
        # Write threshold-1 not-ok, then one ok, then one more not-ok
        _write_n_not_ok(root, task_id, threshold - 1)
        write_iteration_event(
            root, task_id, "test",
            verdict_ok=True, verdict_reason="criterion satisfied",
        )
        write_iteration_event(
            root, task_id, "test",
            verdict_ok=False, verdict_reason="new criterion unmet",
        )
        # Only 1 trailing not-ok; below threshold
        assert detect_thrashing(root, task_id, "test", threshold=threshold) is False

    def test_does_not_fire_on_impossible(self, temp_portfolio):
        """impossible=True iterations are NOT counted as stalled not-ok."""
        root = temp_portfolio["root"]
        task_id = "TEST-004"
        threshold = 3
        # Fill with not-ok then one impossible -- impossible breaks the run
        _write_n_not_ok(root, task_id, threshold - 1)
        write_iteration_event(
            root, task_id, "test",
            verdict_ok=False, verdict_reason="genuinely blocked",
            verdict_impossible=True,
        )
        assert detect_thrashing(root, task_id, "test", threshold=threshold) is False

    def test_exactly_threshold_fires(self, temp_portfolio):
        """Exactly K consecutive not-ok triggers thrashing (inclusive)."""
        root = temp_portfolio["root"]
        task_id = "TEST-005"
        threshold = 4
        _write_n_not_ok(root, task_id, threshold)
        assert detect_thrashing(root, task_id, "test", threshold=threshold) is True

    def test_no_file_returns_false(self, temp_portfolio):
        """No reflection file = no iterations = no thrashing."""
        root = temp_portfolio["root"]
        assert detect_thrashing(root, "NONEXISTENT-001", "test", threshold=3) is False

    def test_ok_iteration_resets_run(self, temp_portfolio):
        """An ok iteration resets the consecutive count."""
        root = temp_portfolio["root"]
        task_id = "TEST-006"
        threshold = 3
        # 3 not-ok (would trip), then ok (resets), then 2 not-ok (below threshold)
        _write_n_not_ok(root, task_id, threshold)
        write_iteration_event(
            root, task_id, "test",
            verdict_ok=True, verdict_reason="fixed",
        )
        _write_n_not_ok(root, task_id, threshold - 1)
        assert detect_thrashing(root, task_id, "test", threshold=threshold) is False

    def test_fire_after_ok_then_enough_not_ok(self, temp_portfolio):
        """After an ok, a new run of K not-ok should trip again."""
        root = temp_portfolio["root"]
        task_id = "TEST-007"
        threshold = 3
        # ok, then threshold not-ok
        write_iteration_event(
            root, task_id, "test",
            verdict_ok=True, verdict_reason="first pass fine",
        )
        _write_n_not_ok(root, task_id, threshold)
        assert detect_thrashing(root, task_id, "test", threshold=threshold) is True


# ---------------------------------------------------------------------------
# Per-task threshold via Predictions.thrash_threshold
# ---------------------------------------------------------------------------


class TestPerTaskThreshold:
    def test_predictions_carries_thrash_threshold(self):
        """Predictions.thrash_threshold is stored and round-trips through to_dict/from_dict."""
        p = Predictions(thrash_threshold=5)
        d = p.to_dict()
        assert d["thrash_threshold"] == 5
        p2 = Predictions.from_dict(d)
        assert p2.thrash_threshold == 5

    def test_predictions_default_none(self):
        p = Predictions()
        assert p.thrash_threshold is None
        assert p.to_dict()["thrash_threshold"] is None

    def test_detect_thrashing_respects_per_task_threshold(self, temp_portfolio):
        """Lower per-task threshold fires sooner than global default."""
        root = temp_portfolio["root"]
        task_id = "TEST-008"
        per_task_threshold = 2
        global_default = 4
        _write_n_not_ok(root, task_id, per_task_threshold)
        # Per-task threshold fires
        assert detect_thrashing(root, task_id, "test", threshold=per_task_threshold) is True
        # Global default would NOT have fired yet
        assert detect_thrashing(root, task_id, "test", threshold=global_default) is False

    def test_detect_thrashing_is_pure_ignores_env(self, temp_portfolio, monkeypatch):
        """detect_thrashing trusts its threshold argument and NEVER consults
        the env var -- the caller owns precedence. A threshold EQUAL to the
        module default must not be silently overridden by CLAWPM_THRASH_THRESHOLD."""
        from clawpm.reflect import _DEFAULT_THRASH_THRESHOLD

        monkeypatch.setenv("CLAWPM_THRASH_THRESHOLD", "10")
        root = temp_portfolio["root"]
        task_id = "TEST-PURE-001"
        # Write exactly _DEFAULT_THRASH_THRESHOLD not-ok iterations.
        _write_n_not_ok(root, task_id, _DEFAULT_THRASH_THRESHOLD)
        # Passing threshold == the default must still trip at the default,
        # NOT defer to the env var's 10 (which would need 10 iterations).
        assert detect_thrashing(
            root, task_id, "test", threshold=_DEFAULT_THRASH_THRESHOLD
        ) is True


# ---------------------------------------------------------------------------
# Global env-var default (CLAWPM_THRASH_THRESHOLD)
# ---------------------------------------------------------------------------


class TestGlobalEnvThreshold:
    def test_env_var_controls_default_threshold(self, temp_portfolio, monkeypatch):
        """CLAWPM_THRASH_THRESHOLD env var is respected by the hook path."""
        monkeypatch.setenv("CLAWPM_THRASH_THRESHOLD", "2")

        root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        task = add_task(
            config, "test", title="Env threshold test",
            predictions=Predictions(success_criteria=["c1"]),
        )
        transcript_file = root / "transcript.txt"
        transcript_file.write_text("FAILED", encoding="utf-8")

        import clawpm.judges.stop_condition as sc_mod
        from clawpm.judges.stop_condition import JudgeVerdict

        def fake_eval(rubric: str, transcript: str, invoker=None):
            return JudgeVerdict(ok=False, reason="criterion not yet met")

        monkeypatch.setattr(sc_mod, "evaluate_stop_condition", fake_eval)
        runner = CliRunner()

        # Invoke eval-stop enough times to trip the env threshold
        outputs = []
        for _ in range(2):
            r = runner.invoke(
                main,
                ["-p", "test", "hook", "eval-stop",
                 "--task", task.id,
                 "--transcript-file", str(transcript_file)],
            )
            assert r.exit_code == 0, r.output
            outputs.append(json.loads(r.output))

        # Last output should be stop_condition_tripped (thrashing), not block
        last = outputs[-1]
        assert last.get("continue") is True
        assert "THRASHING" in last.get("systemMessage", "")

    def test_progress_each_iter_never_trips_env_threshold(self, temp_portfolio, monkeypatch):
        """Even with a low env threshold, making progress prevents thrashing flag."""
        monkeypatch.setenv("CLAWPM_THRASH_THRESHOLD", "2")

        root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        task = add_task(
            config, "test", title="Progress test",
            predictions=Predictions(success_criteria=["c1"]),
        )
        transcript_file = root / "transcript2.txt"
        transcript_file.write_text("PASSED", encoding="utf-8")

        import clawpm.judges.stop_condition as sc_mod
        from clawpm.judges.stop_condition import JudgeVerdict
        call_count = [0]

        def alternating_eval(rubric: str, transcript: str, invoker=None):
            call_count[0] += 1
            # Alternates: ok, not-ok, ok, not-ok -- consecutive run never reaches 2
            if call_count[0] % 2 == 1:
                return JudgeVerdict(ok=True, reason="criterion satisfied")
            return JudgeVerdict(ok=False, reason="minor gap")

        monkeypatch.setattr(sc_mod, "evaluate_stop_condition", alternating_eval)
        runner = CliRunner()

        outputs = []
        for _ in range(4):
            r = runner.invoke(
                main,
                ["-p", "test", "hook", "eval-stop",
                 "--task", task.id,
                 "--transcript-file", str(transcript_file)],
            )
            assert r.exit_code == 0, r.output
            outputs.append(json.loads(r.output))

        # None of the outputs should mention THRASHING
        for out in outputs:
            assert "THRASHING" not in out.get("systemMessage", "")

    def test_per_task_threshold_equal_to_default_wins_over_env(
        self, temp_portfolio, monkeypatch
    ):
        """Config-hierarchy guard: a per-task thrash_threshold that happens to
        EQUAL the module default must NOT be overridden by a (larger) env var.
        With per-task=4 and CLAWPM_THRASH_THRESHOLD=10, detection must trip at
        4 iterations, not wait for 10."""
        from clawpm.reflect import _DEFAULT_THRASH_THRESHOLD

        monkeypatch.setenv("CLAWPM_THRASH_THRESHOLD", "10")

        root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        # Per-task threshold deliberately equals the module default.
        per_task = _DEFAULT_THRASH_THRESHOLD
        task = add_task(
            config, "test", title="Equal-to-default threshold",
            predictions=Predictions(
                success_criteria=["c1"],
                thrash_threshold=per_task,
            ),
        )
        transcript_file = root / "transcript_equal.txt"
        transcript_file.write_text("FAILED", encoding="utf-8")

        import clawpm.judges.stop_condition as sc_mod
        from clawpm.judges.stop_condition import JudgeVerdict

        def fake_eval(rubric: str, transcript: str, invoker=None):
            return JudgeVerdict(ok=False, reason="criterion not yet met")

        monkeypatch.setattr(sc_mod, "evaluate_stop_condition", fake_eval)
        runner = CliRunner()

        outputs = []
        for _ in range(per_task):
            r = runner.invoke(
                main,
                ["-p", "test", "hook", "eval-stop",
                 "--task", task.id,
                 "--transcript-file", str(transcript_file)],
            )
            assert r.exit_code == 0, r.output
            outputs.append(json.loads(r.output))

        # The first per_task-1 are normal blocks (env's 10 would NOT have
        # fired yet either, so this proves per-task is what's in effect).
        for out in outputs[:-1]:
            assert out.get("decision") == "block", f"expected block, got {out}"
        # The per_task-th iteration trips thrashing -- per-task threshold won.
        last = outputs[-1]
        assert last.get("continue") is True, f"expected continue, got {last}"
        assert "THRASHING" in last.get("systemMessage", "")


# ---------------------------------------------------------------------------
# CLI integration: hook eval-stop surfaces stop_condition_tripped for thrashing
# ---------------------------------------------------------------------------


class TestHookEvalStopThrashing:
    def test_eval_stop_trips_stop_condition_at_threshold(
        self, temp_portfolio, monkeypatch
    ):
        """hook eval-stop emits continue=true + THRASHING message after K not-ok iterations."""
        root = temp_portfolio["root"]
        config = temp_portfolio["config"]

        threshold = 3
        task = add_task(
            config, "test", title="Thrash integration",
            predictions=Predictions(
                success_criteria=["all tests pass"],
                thrash_threshold=threshold,
            ),
        )
        transcript_file = root / "t_integration.txt"
        transcript_file.write_text("tests still failing", encoding="utf-8")

        import clawpm.judges.stop_condition as sc_mod
        from clawpm.judges.stop_condition import JudgeVerdict

        def failing_eval(rubric: str, transcript: str, invoker=None):
            return JudgeVerdict(ok=False, reason="tests still failing")

        monkeypatch.setattr(sc_mod, "evaluate_stop_condition", failing_eval)
        runner = CliRunner()

        outputs = []
        for _ in range(threshold):
            r = runner.invoke(
                main,
                ["-p", "test", "hook", "eval-stop",
                 "--task", task.id,
                 "--transcript-file", str(transcript_file)],
            )
            assert r.exit_code == 0, r.output
            outputs.append(json.loads(r.output))

        # First threshold-1 should be normal block decisions
        for out in outputs[:-1]:
            assert out.get("decision") == "block", f"expected block, got {out}"
            assert "THRASHING" not in out.get("reason", "")

        # The Kth iteration trips thrashing
        final = outputs[-1]
        assert final.get("continue") is True, f"expected continue=true, got {final}"
        msg = final.get("systemMessage", "")
        assert "THRASHING" in msg

    def test_eval_stop_thrash_is_distinct_from_impossible(
        self, temp_portfolio, monkeypatch
    ):
        """Thrashing verdict is distinctly labelled, not mixed with impossible."""
        root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        threshold = 2
        task = add_task(
            config, "test", title="Distinct thrash",
            predictions=Predictions(
                success_criteria=["criterion"],
                thrash_threshold=threshold,
            ),
        )
        transcript_file = root / "t_distinct.txt"
        transcript_file.write_text("failing", encoding="utf-8")

        import clawpm.judges.stop_condition as sc_mod
        from clawpm.judges.stop_condition import JudgeVerdict

        def failing_eval(rubric: str, transcript: str, invoker=None):
            return JudgeVerdict(ok=False, reason="not done")

        monkeypatch.setattr(sc_mod, "evaluate_stop_condition", failing_eval)
        runner = CliRunner()

        for _ in range(threshold):
            r = runner.invoke(
                main,
                ["-p", "test", "hook", "eval-stop",
                 "--task", task.id,
                 "--transcript-file", str(transcript_file)],
            )
            assert r.exit_code == 0

        last = json.loads(r.output)
        # Explicitly not "impossible" in the message
        msg = last.get("systemMessage", "")
        assert "impossible" not in msg.lower()
        assert "THRASHING" in msg

    def test_progress_prevents_thrash_flag(
        self, temp_portfolio, monkeypatch
    ):
        """An ok verdict resets the counter; K later not-ok must restart the count."""
        root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        threshold = 3
        task = add_task(
            config, "test", title="Progress then stall",
            predictions=Predictions(
                success_criteria=["criterion"],
                thrash_threshold=threshold,
            ),
        )
        transcript_file = root / "t_progress.txt"
        transcript_file.write_text("mixed", encoding="utf-8")

        import clawpm.judges.stop_condition as sc_mod
        from clawpm.judges.stop_condition import JudgeVerdict
        eval_results = [
            JudgeVerdict(ok=False, reason="nope"),
            JudgeVerdict(ok=False, reason="nope"),
            JudgeVerdict(ok=True, reason="fixed it"),    # resets counter
            JudgeVerdict(ok=False, reason="new issue"),  # counter restarts: 1
            JudgeVerdict(ok=False, reason="new issue"),  # counter: 2
        ]
        call_count = [0]

        def rotating_eval(rubric: str, transcript: str, invoker=None):
            idx = min(call_count[0], len(eval_results) - 1)
            call_count[0] += 1
            return eval_results[idx]

        monkeypatch.setattr(sc_mod, "evaluate_stop_condition", rotating_eval)
        runner = CliRunner()

        outputs = []
        for _ in range(len(eval_results)):
            r = runner.invoke(
                main,
                ["-p", "test", "hook", "eval-stop",
                 "--task", task.id,
                 "--transcript-file", str(transcript_file)],
            )
            assert r.exit_code == 0
            outputs.append(json.loads(r.output))

        # No output should trip thrashing -- counter resets at ok=True (iter 3)
        for out in outputs:
            assert "THRASHING" not in out.get("systemMessage", "")
            assert "THRASHING" not in out.get("reason", "")
