"""Tests for the dispatch iteration cap (CLAWP-070).

CLAWP-070 evaluated a standalone ``clawpm loop`` command and folded the one
missing knob into dispatch instead (see docs/design/loop.md): an absolute
``--max-iterations`` cap on the Stop-hook rubric loop. One test per
termination path:

1. rubric satisfied  -> loop ends (ok verdict closes, even at the cap)
2. max-iterations    -> cap trips a STOP_CONDITION_TRIPPED stop
3. thrashing         -> still handled by CLAWP-062, independent of the cap
Plus the wiring: dispatch bakes the cap + a per-dispatch baseline into the
Stop-hook command so a re-dispatch gets a fresh budget.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.dispatch import build_settings_payload
from clawpm.judges.stop_condition import JudgeVerdict
from clawpm.models import Predictions
from clawpm.reflect import write_iteration_event
from clawpm.tasks import add_task


@pytest.fixture
def temp_portfolio(isolated_portfolio):
    return {"root": isolated_portfolio.root, "config": isolated_portfolio.config}


def _stop_cmd(payload: dict) -> str:
    return payload["hooks"]["Stop"][0]["hooks"][0]["command"]


def _invoke_eval_stop(task_id, transcript_file, *extra):
    r = CliRunner().invoke(
        main,
        ["-p", "test", "hook", "eval-stop", "--task", task_id,
         "--transcript-file", str(transcript_file), *extra],
    )
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


@pytest.fixture
def transcript(temp_portfolio):
    f = temp_portfolio["root"] / "t.txt"
    f.write_text("work in progress", encoding="utf-8")
    return f


def _patch_judge(monkeypatch, verdicts):
    """Judge returns the given verdicts in order (last one repeats)."""
    import clawpm.judges.stop_condition as sc_mod

    seq = list(verdicts)

    def fake(rubric, transcript, invoker=None):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(sc_mod, "evaluate_stop_condition", fake)


class TestPayloadWiring:
    def test_default_omits_cap(self):
        cmd = _stop_cmd(build_settings_payload("TEST-001", "test"))
        assert "--max-iterations" not in cmd
        assert "--iteration-baseline" not in cmd

    def test_cap_and_baseline_baked_into_stop_command(self):
        cmd = _stop_cmd(
            build_settings_payload(
                "TEST-001", "test", max_iterations=3, iteration_baseline=2
            )
        )
        assert "--max-iterations 3" in cmd
        assert "--iteration-baseline 2" in cmd

    def test_non_positive_cap_rejected(self):
        with pytest.raises(ValueError):
            build_settings_payload("TEST-001", "test", max_iterations=0)

    def test_cli_dispatch_flag_reaches_stop_command(self, temp_portfolio, tmp_path):
        task = add_task(
            temp_portfolio["config"], "test", title="cap", description="b",
            predictions=Predictions(success_criteria=["c"], filled_by="agent"),
        )
        # Two prior iterations from an earlier dispatch must become the baseline.
        for _ in range(2):
            write_iteration_event(
                temp_portfolio["root"], task.id, "test",
                verdict_ok=False, verdict_reason="earlier dispatch",
            )
        target = tmp_path / "disp"
        r = CliRunner().invoke(
            main,
            ["tasks", "dispatch", task.id, "--project", "test",
             "--target-dir", str(target), "--no-session-context",
             "--max-iterations", "5"],
        )
        assert r.exit_code == 0, r.output
        payload = json.loads(
            (target / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )
        cmd = _stop_cmd(payload)
        assert "--max-iterations 5" in cmd
        assert "--iteration-baseline 2" in cmd

    def test_cli_dispatch_rejects_zero_cap(self, temp_portfolio, tmp_path):
        task = add_task(
            temp_portfolio["config"], "test", title="cap0", description="b",
            predictions=Predictions(success_criteria=["c"], filled_by="agent"),
        )
        r = CliRunner().invoke(
            main,
            ["tasks", "dispatch", task.id, "--project", "test",
             "--target-dir", str(tmp_path / "d0"), "--no-session-context",
             "--max-iterations", "0"],
        )
        assert r.exit_code != 0


class TestTerminationPaths:
    def test_cap_trips_after_n_not_ok_iterations(
        self, temp_portfolio, transcript, monkeypatch
    ):
        # Thrash threshold set high so only the cap can end this run.
        task = add_task(
            temp_portfolio["config"], "test", title="cap trips",
            predictions=Predictions(success_criteria=["c"], thrash_threshold=50),
        )
        _patch_judge(monkeypatch, [JudgeVerdict(ok=False, reason="still failing")])
        outs = [
            _invoke_eval_stop(task.id, transcript, "--max-iterations", "3")
            for _ in range(3)
        ]
        for out in outs[:2]:
            assert out.get("decision") == "block"
        final = outs[2]
        assert final.get("continue") is True
        assert "MAX_ITERATIONS" in final.get("systemMessage", "")
        assert "THRASHING" not in final.get("systemMessage", "")

    def test_rubric_satisfied_at_cap_still_closes_ok(
        self, temp_portfolio, transcript, monkeypatch
    ):
        task = add_task(
            temp_portfolio["config"], "test", title="ok at cap",
            predictions=Predictions(success_criteria=["c"], thrash_threshold=50),
        )
        _patch_judge(
            monkeypatch,
            [JudgeVerdict(ok=False, reason="no"), JudgeVerdict(ok=True, reason="done")],
        )
        _invoke_eval_stop(task.id, transcript, "--max-iterations", "2")
        final = _invoke_eval_stop(task.id, transcript, "--max-iterations", "2")
        msg = final.get("systemMessage", "")
        assert "rubric satisfied" in msg
        assert "MAX_ITERATIONS" not in msg

    def test_baseline_gives_redispatch_a_fresh_budget(
        self, temp_portfolio, transcript, monkeypatch
    ):
        task = add_task(
            temp_portfolio["config"], "test", title="baseline",
            predictions=Predictions(success_criteria=["c"], thrash_threshold=50),
        )
        for _ in range(4):  # history from an earlier, capped dispatch
            write_iteration_event(
                temp_portfolio["root"], task.id, "test",
                verdict_ok=False, verdict_reason="old",
            )
        _patch_judge(monkeypatch, [JudgeVerdict(ok=False, reason="still failing")])
        first = _invoke_eval_stop(
            task.id, transcript, "--max-iterations", "2", "--iteration-baseline", "4"
        )
        assert first.get("decision") == "block"  # 1 of 2 in THIS dispatch
        second = _invoke_eval_stop(
            task.id, transcript, "--max-iterations", "2", "--iteration-baseline", "4"
        )
        assert "MAX_ITERATIONS" in second.get("systemMessage", "")

    def test_no_cap_means_no_trip(self, temp_portfolio, transcript, monkeypatch):
        task = add_task(
            temp_portfolio["config"], "test", title="uncapped",
            predictions=Predictions(success_criteria=["c"], thrash_threshold=50),
        )
        _patch_judge(monkeypatch, [JudgeVerdict(ok=False, reason="still failing")])
        for _ in range(6):
            out = _invoke_eval_stop(task.id, transcript)
            assert out.get("decision") == "block"

    def test_thrashing_still_wins_independently(
        self, temp_portfolio, transcript, monkeypatch
    ):
        task = add_task(
            temp_portfolio["config"], "test", title="thrash first",
            predictions=Predictions(success_criteria=["c"], thrash_threshold=2),
        )
        _patch_judge(monkeypatch, [JudgeVerdict(ok=False, reason="still failing")])
        _invoke_eval_stop(task.id, transcript, "--max-iterations", "9")
        final = _invoke_eval_stop(task.id, transcript, "--max-iterations", "9")
        assert "THRASHING" in final.get("systemMessage", "")


class TestCapFailsClosedOnLogIOErrors:
    """Codex P2 (PR #87): a cap that cannot be enforced must not keep the
    agent looping. Blocking Stop is what keeps the agent running, so with
    --max-iterations set, an I/O failure lets Stop proceed (tripped path)
    with an enforcement-error message. Without the flag, behaviour is
    unchanged (fail-open: the rubric block stands)."""

    @staticmethod
    def _task(temp_portfolio, title):
        return add_task(
            temp_portfolio["config"], "test", title=title,
            predictions=Predictions(success_criteria=["c"], thrash_threshold=50),
        )

    @staticmethod
    def _break_count(monkeypatch):
        import clawpm.reflect as reflect_mod

        def boom(*a, **k):
            raise OSError("simulated read failure")

        monkeypatch.setattr(reflect_mod, "count_iterations_for_task", boom)

    @staticmethod
    def _break_write(monkeypatch):
        import clawpm.reflect as reflect_mod

        def boom(*a, **k):
            raise OSError("simulated write failure")

        monkeypatch.setattr(reflect_mod, "write_iteration_event", boom)

    def _assert_tripped_enforcement_error(self, out, needle):
        assert out.get("decision") != "block"
        assert out.get("continue") is True
        msg = out.get("systemMessage", "")
        assert "STOP_CONDITION_TRIPPED" in msg
        assert "enforcement error" in msg.lower()
        assert "MAX_ITERATIONS" in msg
        assert needle in msg

    def test_count_read_failure_with_cap_allows_stop(
        self, temp_portfolio, transcript, monkeypatch
    ):
        task = self._task(temp_portfolio, "count read fail")
        _patch_judge(monkeypatch, [JudgeVerdict(ok=False, reason="still failing")])
        self._break_count(monkeypatch)
        out = _invoke_eval_stop(task.id, transcript, "--max-iterations", "3")
        self._assert_tripped_enforcement_error(out, "simulated read failure")

    def test_write_failure_with_cap_allows_stop(
        self, temp_portfolio, transcript, monkeypatch
    ):
        task = self._task(temp_portfolio, "write fail")
        _patch_judge(monkeypatch, [JudgeVerdict(ok=False, reason="still failing")])
        self._break_write(monkeypatch)
        out = _invoke_eval_stop(task.id, transcript, "--max-iterations", "3")
        self._assert_tripped_enforcement_error(out, "simulated write failure")

    def test_write_failure_ok_verdict_unchanged_with_cap(
        self, temp_portfolio, transcript, monkeypatch
    ):
        task = self._task(temp_portfolio, "write fail ok")
        _patch_judge(monkeypatch, [JudgeVerdict(ok=True, reason="done")])
        self._break_write(monkeypatch)
        out = _invoke_eval_stop(task.id, transcript, "--max-iterations", "3")
        msg = out.get("systemMessage", "")
        assert out.get("continue") is True
        assert "rubric satisfied" in msg
        assert "WRITE FAILED" in msg
        assert "MAX_ITERATIONS" not in msg

    def test_write_failure_impossible_verdict_unchanged_with_cap(
        self, temp_portfolio, transcript, monkeypatch
    ):
        task = self._task(temp_portfolio, "write fail impossible")
        _patch_judge(
            monkeypatch,
            [JudgeVerdict(ok=False, impossible=True, reason="cannot be done")],
        )
        self._break_write(monkeypatch)
        out = _invoke_eval_stop(task.id, transcript, "--max-iterations", "3")
        msg = out.get("systemMessage", "")
        assert out.get("continue") is True
        assert "IMPOSSIBLE" in msg
        assert "MAX_ITERATIONS" not in msg

    def test_count_failure_ok_and_impossible_unchanged(
        self, temp_portfolio, transcript, monkeypatch
    ):
        task = self._task(temp_portfolio, "count fail ok")
        self._break_count(monkeypatch)
        _patch_judge(monkeypatch, [JudgeVerdict(ok=True, reason="done")])
        ok_out = _invoke_eval_stop(task.id, transcript, "--max-iterations", "3")
        assert "rubric satisfied" in ok_out.get("systemMessage", "")
        assert "MAX_ITERATIONS" not in ok_out.get("systemMessage", "")
        _patch_judge(
            monkeypatch,
            [JudgeVerdict(ok=False, impossible=True, reason="cannot be done")],
        )
        imp_out = _invoke_eval_stop(task.id, transcript, "--max-iterations", "3")
        assert "IMPOSSIBLE" in imp_out.get("systemMessage", "")
        assert "MAX_ITERATIONS" not in imp_out.get("systemMessage", "")

    def test_write_failure_without_cap_still_blocks(
        self, temp_portfolio, transcript, monkeypatch
    ):
        task = self._task(temp_portfolio, "write fail no cap")
        _patch_judge(monkeypatch, [JudgeVerdict(ok=False, reason="still failing")])
        self._break_write(monkeypatch)
        out = _invoke_eval_stop(task.id, transcript)
        assert out.get("decision") == "block"
        assert "MAX_ITERATIONS" not in json.dumps(out)

    def test_count_failure_without_cap_still_blocks(
        self, temp_portfolio, transcript, monkeypatch
    ):
        task = self._task(temp_portfolio, "count fail no cap")
        _patch_judge(monkeypatch, [JudgeVerdict(ok=False, reason="still failing")])
        self._break_count(monkeypatch)
        out = _invoke_eval_stop(task.id, transcript)
        assert out.get("decision") == "block"
