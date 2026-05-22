"""Tests for the Stop-hook condition evaluator (CLAWP-017).

Adversarial coverage:
  - Genuine done — judge returns ok=true → continue=true
  - Subagent falsely claims done — judge independently rejects → continue=false
  - Genuine impossibility — judge returns impossible=true → continue=true
    with operator-triage systemMessage (do NOT loop forever)
  - Judge output malformed (truncation, JSON-in-fence, prose preamble)
  - Judge subprocess unavailable / times out / errors
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
from clawpm.judges.stop_condition import (
    JudgeVerdict,
    build_judge_prompt,
    evaluate_stop_condition,
    map_verdict_to_hook_output,
)
from clawpm.models import Predictions, SuccessCriterion
from clawpm.tasks import add_task


# ---------------------------------------------------------------------------
# Shared fixture (reused from test_rubric)
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_stop_test_")
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


# ---------------------------------------------------------------------------
# JudgeVerdict.parse — defensive
# ---------------------------------------------------------------------------


class TestVerdictParse:
    def test_pure_ok_true(self):
        v = JudgeVerdict.parse('{"ok": true, "reason": "did it"}')
        assert v.ok is True
        assert v.reason == "did it"
        assert v.impossible is False

    def test_pure_ok_false(self):
        v = JudgeVerdict.parse('{"ok": false, "reason": "no evidence"}')
        assert v.ok is False
        assert v.impossible is False

    def test_impossible(self):
        v = JudgeVerdict.parse(
            '{"ok": false, "impossible": true, "reason": "rubric self-contradicts"}'
        )
        assert v.ok is False
        assert v.impossible is True

    def test_json_in_code_fence(self):
        v = JudgeVerdict.parse(
            '```json\n{"ok": true, "reason": "fenced"}\n```'
        )
        assert v.ok is True
        assert v.reason == "fenced"

    def test_prose_preamble(self):
        v = JudgeVerdict.parse(
            'Here is my verdict:\n{"ok": false, "reason": "missing X"}\nDone.'
        )
        assert v.ok is False
        assert v.reason == "missing X"

    def test_malformed_returns_not_ok(self):
        """Parse failures must NOT default to ok=true (would let agents stop)."""
        v = JudgeVerdict.parse("this is not json at all")
        assert v.ok is False
        assert "unparseable" in v.reason.lower()

    def test_truncated_returns_not_ok(self):
        v = JudgeVerdict.parse('{"ok": true, "reason":')
        assert v.ok is False

    def test_contradiction_ok_true_and_impossible(self):
        """If judge returns both ok=true AND impossible=true, treat as not-ok."""
        v = JudgeVerdict.parse(
            '{"ok": true, "impossible": true, "reason": "weird"}'
        )
        assert v.ok is False
        assert v.impossible is True
        assert "contradiction" in v.reason.lower()

    def test_no_braces_returns_not_ok(self):
        """No `{` present at all — array, scalar, prose."""
        v = JudgeVerdict.parse('["ok"]')
        assert v.ok is False
        assert "no json object" in v.reason.lower()


# ---------------------------------------------------------------------------
# evaluate_stop_condition with injected invoker
# ---------------------------------------------------------------------------


class TestEvaluateWithInjector:
    def test_genuine_done(self):
        """Subagent transcript shows real evidence → judge says ok=true."""
        def fake_judge(prompt: str) -> str:
            assert "RUBRIC" in prompt
            assert "TRANSCRIPT" in prompt
            return '{"ok": true, "reason": "tests pass per transcript line 42"}'

        v = evaluate_stop_condition(
            rubric="All tests pass.",
            transcript="$ pytest\n=== 100 passed ===",
            invoker=fake_judge,
        )
        assert v.ok is True

    def test_subagent_falsely_claims_done(self):
        """Subagent claims done but evidence absent → judge ok=false."""
        def fake_judge(prompt: str) -> str:
            # Even though the transcript SAYS done, the judge is supposed to
            # independently verify.
            return ('{"ok": false, "reason": "transcript contains no evidence '
                    'tests were actually run"}')

        v = evaluate_stop_condition(
            rubric="All tests pass.",
            transcript="I claim all tests pass. Done.",  # no actual evidence
            invoker=fake_judge,
        )
        assert v.ok is False
        assert "no evidence" in v.reason

    def test_genuine_impossibility(self):
        def fake_judge(prompt: str) -> str:
            return ('{"ok": false, "impossible": true, '
                    '"reason": "rubric requires AWS credentials we do not have"}')

        v = evaluate_stop_condition(
            rubric="Deploy to AWS prod.",
            transcript="$ aws sts get-caller-identity\nNotAuthorized",
            invoker=fake_judge,
        )
        assert v.ok is False
        assert v.impossible is True

    def test_invoker_receives_full_rubric_and_transcript(self):
        captured: list[str] = []
        def fake_judge(prompt: str) -> str:
            captured.append(prompt)
            return '{"ok": true, "reason": "ok"}'

        evaluate_stop_condition(
            rubric="RUBRIC_TEXT_PRESENT",
            transcript="TRANSCRIPT_TEXT_PRESENT",
            invoker=fake_judge,
        )
        assert "RUBRIC_TEXT_PRESENT" in captured[0]
        assert "TRANSCRIPT_TEXT_PRESENT" in captured[0]
        # Piebald doctrine string must appear in the prompt
        assert "evidence, not proof" in captured[0]


# ---------------------------------------------------------------------------
# Hook output mapping
# ---------------------------------------------------------------------------


class TestHookOutputMapping:
    def test_ok_true_lets_agent_stop(self):
        v = JudgeVerdict(ok=True, reason="rubric met")
        out = map_verdict_to_hook_output(v)
        assert out["continue"] is True
        assert "satisfied" in out["systemMessage"]

    def test_ok_false_blocks_stop(self):
        v = JudgeVerdict(ok=False, reason="missing X")
        out = map_verdict_to_hook_output(v)
        assert out["continue"] is False
        assert out["decision"] == "block"
        assert "missing X" in out["stopReason"]

    def test_impossible_lets_agent_stop_but_flags_operator(self):
        """Impossibility must NOT loop forever — let the agent stop, surface to operator."""
        v = JudgeVerdict(ok=False, impossible=True, reason="no AWS creds")
        out = map_verdict_to_hook_output(v)
        assert out["continue"] is True
        assert "IMPOSSIBLE" in out["systemMessage"]
        assert "no AWS creds" in out["systemMessage"]


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestCLIEvalStop:
    def test_cli_with_transcript_file_and_canned_judge(
        self, temp_portfolio, monkeypatch
    ):
        # Stub the judge so the test doesn't require an installed claude CLI.
        from clawpm.judges import stop_condition as sc_mod

        def fake_invoker(prompt: str) -> str:
            return '{"ok": true, "reason": "stubbed pass"}'

        monkeypatch.setattr(sc_mod, "_default_judge_invoker", fake_invoker)

        # Make a task with a rubric
        config = temp_portfolio["config"]
        task = add_task(
            config, "test", title="Hook target",
            predictions=Predictions(success_criteria=[
                SuccessCriterion(
                    criterion="Implementation lands a green test suite",
                    gradeable_signal="pytest output shows 0 failures",
                )
            ]),
        )

        # Write a fake transcript file
        transcript = temp_portfolio["root"] / "transcript.jsonl"
        transcript.write_text('{"role":"assistant","text":"100 passed"}\n', encoding="utf-8")

        runner = CliRunner()
        r = runner.invoke(
            main,
            ["-p", "test", "hook", "eval-stop",
             "--task", task.id,
             "--transcript-file", str(transcript)],
        )
        assert r.exit_code == 0, r.output
        # The output IS the hook JSON — no envelope wrapping for hook calls.
        out = json.loads(r.output)
        assert out["continue"] is True
        assert "satisfied" in out["systemMessage"]

    def test_cli_blocks_stop_when_judge_says_not_ok(
        self, temp_portfolio, monkeypatch
    ):
        from clawpm.judges import stop_condition as sc_mod

        def fake_invoker(prompt: str) -> str:
            return ('{"ok": false, "reason": "transcript shows tests failing"}')

        monkeypatch.setattr(sc_mod, "_default_judge_invoker", fake_invoker)

        config = temp_portfolio["config"]
        task = add_task(
            config, "test", title="Failing",
            predictions=Predictions(success_criteria=["tests pass"]),
        )
        transcript = temp_portfolio["root"] / "transcript.jsonl"
        transcript.write_text("FAILED", encoding="utf-8")

        r = CliRunner().invoke(
            main,
            ["-p", "test", "hook", "eval-stop",
             "--task", task.id,
             "--transcript-file", str(transcript)],
        )
        assert r.exit_code == 0
        out = json.loads(r.output)
        assert out["continue"] is False
        assert out["decision"] == "block"
        assert "tests failing" in out["stopReason"]

    def test_cli_handles_missing_task_gracefully(self, temp_portfolio):
        """Unknown task must not crash the hook — emit safe continue=true."""
        r = CliRunner().invoke(
            main,
            ["-p", "test", "hook", "eval-stop",
             "--task", "NOPE-001",
             "--transcript-file", "/dev/null"],
            input="",
        )
        # exit 0 because we MUST emit a hook-shaped JSON, not an error envelope
        assert r.exit_code == 0
        out = json.loads(r.output)
        assert out["continue"] is True
        assert "not found" in out["systemMessage"]

    def test_cli_handles_missing_transcript_gracefully(
        self, temp_portfolio, monkeypatch
    ):
        """No transcript + no stdin → continue=true with note, NOT a crash."""
        config = temp_portfolio["config"]
        task = add_task(
            config, "test", title="X",
            predictions=Predictions(success_criteria=["a"]),
        )

        # Click's CliRunner without input= argument sends empty stdin
        r = CliRunner().invoke(
            main,
            ["-p", "test", "hook", "eval-stop", "--task", task.id],
            input="",
        )
        assert r.exit_code == 0
        out = json.loads(r.output)
        assert out["continue"] is True
        assert "not enforced" in out["systemMessage"]


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestPromptBuilding:
    def test_prompt_contains_piebald_doctrine(self):
        prompt = build_judge_prompt(rubric="R", transcript="T")
        assert "evidence, not proof" in prompt
        assert "Return ONLY the JSON object" in prompt
        assert "R" in prompt and "T" in prompt
