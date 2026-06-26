"""Tests for CLAWP-054 — Task/dispatch contract fields.

Covers three new Task schema fields:
  1. out_of_scope   — repeatable boundary list (what NOT to touch)
  2. stop_conditions — repeatable escape-hatch list (STOP + report if X breaks)
  3. delegability   — enum agent|human|either (who may execute)

Success criteria (all must hold):
  A. Tasks accept repeatable --out-of-scope and --stop-condition values;
     persisted and rendered in dispatch/agent preamble VERBATIM.
  B. A tripped stop_condition surfaces to the Stop-hook judge as a terminal
     report-back outcome DISTINCT from an unmet success_criterion.
  C. A human delegability task is REFUSED by the dispatch path.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import Task, TaskState
from clawpm.tasks import add_task, edit_task, get_task


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_portfolio():
    """Minimal portfolio with one project — no git repo needed for most tests."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_contract_test_")
    portfolio_root = Path(temp_dir)
    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n'
    )
    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    proj_dir = projects_dir / "test-proj"
    proj_dir.mkdir()
    dot_proj = proj_dir / ".project"
    dot_proj.mkdir()
    (dot_proj / "settings.toml").write_text(
        'id = "test-proj"\n'
        'name = "Test Project"\n'
        f'repo_path = "{proj_dir.as_posix()}"\n'
    )
    yield portfolio_root


@pytest.fixture
def temp_portfolio_with_repo():
    """Portfolio + a real git repo (needed for dispatch/worktree tests)."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_contract_repo_test_")
    portfolio_root = Path(temp_dir)
    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n'
    )
    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    proj_dir = projects_dir / "test-proj"
    proj_dir.mkdir()
    dot_proj = proj_dir / ".project"
    dot_proj.mkdir()
    (dot_proj / "settings.toml").write_text(
        'id = "test-proj"\n'
        'name = "Test Project"\n'
        f'repo_path = "{proj_dir.as_posix()}"\n'
    )
    # Minimal git repo so create_worktree can proceed
    subprocess.run(["git", "init", "-q", "-b", "main", str(proj_dir)], check=True)
    (proj_dir / "README.md").write_text("hi", encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a",
         "-C", str(proj_dir), "add", "README.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a",
         "-C", str(proj_dir), "commit", "-m", "init"],
        check=True,
    )
    yield portfolio_root, proj_dir


# ---------------------------------------------------------------------------
# A. Schema + persistence: out_of_scope and stop_conditions
# ---------------------------------------------------------------------------

class TestOutOfScopeField:
    def test_task_defaults_out_of_scope_to_empty_list(self, temp_portfolio):
        """Backward-compat: existing task files without out_of_scope load fine."""
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Test task")
        assert task is not None
        assert task.out_of_scope == []

    def test_add_task_persists_out_of_scope(self, temp_portfolio):
        """out_of_scope values passed to add_task survive a round-trip."""
        config = load_portfolio_config(temp_portfolio)
        task = add_task(
            config, "test-proj", "Scoped task",
            out_of_scope=["docs/**", "tests/**", "do not touch auth module"],
        )
        assert task is not None
        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded is not None
        assert reloaded.out_of_scope == ["docs/**", "tests/**", "do not touch auth module"]

    def test_edit_task_persists_out_of_scope(self, temp_portfolio):
        """edit_task replaces out_of_scope."""
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Editable task")
        assert task is not None
        updated = edit_task(
            config, "test-proj", task.id,
            out_of_scope=["src/legacy/**"],
        )
        assert updated is not None
        assert updated.out_of_scope == ["src/legacy/**"]

    def test_edit_task_refuses_unparseable_frontmatter(self, temp_portfolio):
        """edit_task raises instead of silently corrupting when the existing
        frontmatter is unparseable. get_task/Task.from_file read leniently
        (degraded Task), so edit_task is reached; its stricter re-parse must
        refuse rather than rebuild a double-frontmatter, field-wiped file
        (CLAWP-066 / Grok review)."""
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Corruptible task")
        assert task is not None and task.file_path is not None

        # Invalid YAML frontmatter (unbalanced flow sequence).
        bad = "---\nthis: [unbalanced\n---\n# Corruptible task\n\nbody\n"
        task.file_path.write_text(bad, encoding="utf-8")

        with pytest.raises(ValueError, match="unparseable"):
            edit_task(config, "test-proj", task.id, priority=2)

        # File left untouched — no clobber.
        assert task.file_path.read_text(encoding="utf-8") == bad

    def test_edit_task_refuses_unterminated_frontmatter(self, temp_portfolio):
        """edit_task refuses a file that opens '---' with no closing fence rather
        than rebuilding a double-frontmatter, metadata-wiped file (Codex review)."""
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Unterminated task")
        assert task is not None and task.file_path is not None

        # Opens with --- but never closes the fence.
        bad = "---\nid: X\npriority: 3\n# Unterminated task\n\nbody\n"
        task.file_path.write_text(bad, encoding="utf-8")

        with pytest.raises(ValueError, match="unterminated"):
            edit_task(config, "test-proj", task.id, priority=2)

        assert task.file_path.read_text(encoding="utf-8") == bad

    def test_to_dict_includes_out_of_scope(self, temp_portfolio):
        """to_dict exposes out_of_scope for agent introspection."""
        config = load_portfolio_config(temp_portfolio)
        task = add_task(
            config, "test-proj", "Dict task",
            out_of_scope=["infra/**"],
        )
        assert task is not None
        d = task.to_dict()
        assert "out_of_scope" in d
        assert d["out_of_scope"] == ["infra/**"]

    def test_cli_add_out_of_scope(self, temp_portfolio):
        """--out-of-scope repeatable option persists to file."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "tasks", "add",
            "--project", "test-proj",
            "--title", "CLI scoped task",
            "--out-of-scope", "docs/**",
            "--out-of-scope", "infra/**",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert result.exit_code == 0, result.output
        config = load_portfolio_config(temp_portfolio)
        from clawpm.tasks import list_tasks
        tasks = list_tasks(config, "test-proj")
        assert len(tasks) == 1
        assert tasks[0].out_of_scope == ["docs/**", "infra/**"]

    def test_cli_edit_out_of_scope(self, temp_portfolio):
        """--out-of-scope on tasks edit replaces the field."""
        runner = CliRunner()
        runner.invoke(main, [
            "tasks", "add",
            "--project", "test-proj",
            "--title", "Editable CLI task",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        config = load_portfolio_config(temp_portfolio)
        from clawpm.tasks import list_tasks
        task_id = list_tasks(config, "test-proj")[0].id
        result = runner.invoke(main, [
            "tasks", "edit",
            "--project", "test-proj",
            task_id,
            "--out-of-scope", "src/legacy/**",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert result.exit_code == 0, result.output
        reloaded = get_task(config, "test-proj", task_id)
        assert reloaded is not None
        assert reloaded.out_of_scope == ["src/legacy/**"]


class TestStopConditionsField:
    def test_task_defaults_stop_conditions_to_empty_list(self, temp_portfolio):
        """Backward-compat: existing task files without stop_conditions load fine."""
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Simple task")
        assert task is not None
        assert task.stop_conditions == []

    def test_add_task_persists_stop_conditions(self, temp_portfolio):
        """stop_conditions values survive a round-trip."""
        config = load_portfolio_config(temp_portfolio)
        task = add_task(
            config, "test-proj", "Guarded task",
            stop_conditions=[
                "if the migration requires a schema change, STOP and report",
                "if test suite baseline is broken, STOP and report",
            ],
        )
        assert task is not None
        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded is not None
        assert reloaded.stop_conditions == [
            "if the migration requires a schema change, STOP and report",
            "if test suite baseline is broken, STOP and report",
        ]

    def test_edit_task_persists_stop_conditions(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Editable task 2")
        assert task is not None
        updated = edit_task(
            config, "test-proj", task.id,
            stop_conditions=["if API rate limit is hit, STOP and report"],
        )
        assert updated is not None
        assert updated.stop_conditions == ["if API rate limit is hit, STOP and report"]

    def test_to_dict_includes_stop_conditions(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(
            config, "test-proj", "Stop cond task",
            stop_conditions=["if DB is unavailable, STOP"],
        )
        assert task is not None
        d = task.to_dict()
        assert "stop_conditions" in d
        assert d["stop_conditions"] == ["if DB is unavailable, STOP"]

    def test_cli_add_stop_condition(self, temp_portfolio):
        """--stop-condition repeatable option persists to file."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "tasks", "add",
            "--project", "test-proj",
            "--title", "CLI stop task",
            "--stop-condition", "if X fails, STOP",
            "--stop-condition", "if Y unavailable, STOP",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert result.exit_code == 0, result.output
        config = load_portfolio_config(temp_portfolio)
        from clawpm.tasks import list_tasks
        tasks = list_tasks(config, "test-proj")
        assert len(tasks) == 1
        assert tasks[0].stop_conditions == ["if X fails, STOP", "if Y unavailable, STOP"]

    def test_cli_edit_stop_condition(self, temp_portfolio):
        """--stop-condition on tasks edit persists."""
        runner = CliRunner()
        runner.invoke(main, [
            "tasks", "add",
            "--project", "test-proj",
            "--title", "Editable CLI task 2",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        config = load_portfolio_config(temp_portfolio)
        from clawpm.tasks import list_tasks
        task_id = list_tasks(config, "test-proj")[0].id
        result = runner.invoke(main, [
            "tasks", "edit",
            "--project", "test-proj",
            task_id,
            "--stop-condition", "if DB offline, STOP",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert result.exit_code == 0, result.output
        reloaded = get_task(config, "test-proj", task_id)
        assert reloaded is not None
        assert reloaded.stop_conditions == ["if DB offline, STOP"]


# ---------------------------------------------------------------------------
# A (cont.) — preamble injection: out_of_scope + stop_conditions rendered verbatim
# ---------------------------------------------------------------------------

class TestPreambleRendering:
    def test_rubric_includes_out_of_scope_verbatim(self, temp_portfolio):
        """render_rubric_markdown embeds out_of_scope items verbatim."""
        from clawpm.rubric import render_rubric_markdown
        config = load_portfolio_config(temp_portfolio)
        task = add_task(
            config, "test-proj", "Preamble task",
            out_of_scope=["docs/**", "do not refactor auth module"],
        )
        assert task is not None
        rubric = render_rubric_markdown(task)
        assert "docs/**" in rubric
        assert "do not refactor auth module" in rubric
        assert "Out of scope" in rubric or "out_of_scope" in rubric

    def test_rubric_includes_stop_conditions_verbatim(self, temp_portfolio):
        """render_rubric_markdown embeds stop_conditions items verbatim."""
        from clawpm.rubric import render_rubric_markdown
        config = load_portfolio_config(temp_portfolio)
        task = add_task(
            config, "test-proj", "Stop cond preamble task",
            stop_conditions=["if migration needs schema change, STOP and report"],
        )
        assert task is not None
        rubric = render_rubric_markdown(task)
        assert "if migration needs schema change, STOP and report" in rubric
        assert "Stop condition" in rubric or "stop_condition" in rubric

    def test_rubric_omits_sections_when_empty(self, temp_portfolio):
        """No Out-of-scope or Stop-conditions section when fields are empty."""
        from clawpm.rubric import render_rubric_markdown
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Plain task")
        assert task is not None
        rubric = render_rubric_markdown(task)
        assert "Out of scope" not in rubric
        assert "Stop condition" not in rubric

    def test_session_start_sidecar_includes_out_of_scope(self, temp_portfolio_with_repo):
        """write_session_start_sidecar embeds out_of_scope from the rubric."""
        from clawpm.dispatch import write_session_start_sidecar
        from clawpm.rubric import render_rubric_markdown
        portfolio_root, proj_dir = temp_portfolio_with_repo
        config = load_portfolio_config(portfolio_root)
        task = add_task(
            config, "test-proj", "Sidecar out-of-scope task",
            out_of_scope=["infra/**", "do not touch billing"],
        )
        assert task is not None
        rubric = render_rubric_markdown(task)
        sidecar_path = write_session_start_sidecar(proj_dir, rubric)
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        ctx = sidecar["hookSpecificOutput"]["additionalContext"]
        assert "infra/**" in ctx
        assert "do not touch billing" in ctx

    def test_session_start_sidecar_includes_stop_conditions(self, temp_portfolio_with_repo):
        """write_session_start_sidecar embeds stop_conditions from the rubric."""
        from clawpm.dispatch import write_session_start_sidecar
        from clawpm.rubric import render_rubric_markdown
        portfolio_root, proj_dir = temp_portfolio_with_repo
        config = load_portfolio_config(portfolio_root)
        task = add_task(
            config, "test-proj", "Sidecar stop-cond task",
            stop_conditions=["if schema migration needed, STOP and report"],
        )
        assert task is not None
        rubric = render_rubric_markdown(task)
        sidecar_path = write_session_start_sidecar(proj_dir, rubric)
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        ctx = sidecar["hookSpecificOutput"]["additionalContext"]
        assert "if schema migration needed, STOP and report" in ctx


# ---------------------------------------------------------------------------
# B. Stop-condition trip: report-back outcome DISTINCT from unmet criterion
# ---------------------------------------------------------------------------

class TestStopConditionTrip:
    """A declared stop_condition trip must surface as a terminal report-back
    outcome that is DISTINCT from an unmet success_criterion.

    Routing:
      - Unmet criterion (ok=False, impossible=False, stop_condition_tripped=False)
        -> decision: block (force another iterate loop)
      - Report-back (stop_condition_tripped=True)
        -> continue: True with a STOP_CONDITION_TRIPPED systemMessage
        -> DISTINCT from impossible (different field, different message prefix)

    The executor DECLARES the trip; the judge does NOT infer it.
    """

    def test_report_back_verdict_is_distinct_from_impossible(self):
        """JudgeVerdict with stop_condition_tripped=True is distinct from impossible=True."""
        from clawpm.judges.stop_condition import JudgeVerdict, map_verdict_to_hook_output
        v_impossible = JudgeVerdict(ok=False, reason="cannot achieve", impossible=True)
        v_report_back = JudgeVerdict(
            ok=False,
            reason="STOP_CONDITION_TRIPPED: schema migration needed",
            stop_condition_tripped=True,
        )
        assert not v_impossible.stop_condition_tripped
        assert not v_report_back.impossible
        out_impossible = map_verdict_to_hook_output(v_impossible)
        out_report_back = map_verdict_to_hook_output(v_report_back)
        # Both let the agent stop
        assert out_impossible.get("continue") is True
        assert out_report_back.get("continue") is True
        # But with different message content
        msg_impossible = out_impossible.get("systemMessage", "")
        msg_report_back = out_report_back.get("systemMessage", "")
        assert "impossible" in msg_impossible.lower() or "IMPOSSIBLE" in msg_impossible
        assert "STOP_CONDITION_TRIPPED" in msg_report_back or "stop_condition" in msg_report_back.lower()

    def test_report_back_not_same_as_unmet_criterion(self):
        """Unmet criterion blocks the stop event; report-back lets it through."""
        from clawpm.judges.stop_condition import JudgeVerdict, map_verdict_to_hook_output
        v_unmet = JudgeVerdict(ok=False, reason="test suite not run")
        v_report_back = JudgeVerdict(
            ok=False,
            reason="STOP_CONDITION_TRIPPED: x",
            stop_condition_tripped=True,
        )
        out_unmet = map_verdict_to_hook_output(v_unmet)
        out_report_back = map_verdict_to_hook_output(v_report_back)
        assert out_unmet.get("decision") == "block"
        assert out_report_back.get("continue") is True
        assert "decision" not in out_report_back

    def test_parse_stop_condition_tripped_from_judge_json(self):
        """JudgeVerdict.parse recognises stop_condition_tripped in judge output."""
        from clawpm.judges.stop_condition import JudgeVerdict
        raw = '{"ok": false, "stop_condition_tripped": true, "reason": "STOP_CONDITION_TRIPPED: migration needed"}'
        v = JudgeVerdict.parse(raw)
        assert not v.ok
        assert v.stop_condition_tripped
        assert not v.impossible

    def test_to_dict_includes_stop_condition_tripped_when_set(self):
        """to_dict emits stop_condition_tripped only when True."""
        from clawpm.judges.stop_condition import JudgeVerdict
        v_plain = JudgeVerdict(ok=False, reason="test not run")
        v_trip = JudgeVerdict(ok=False, reason="tripped", stop_condition_tripped=True)
        assert "stop_condition_tripped" not in v_plain.to_dict()
        assert v_trip.to_dict()["stop_condition_tripped"] is True

    def test_direct_construction_rejects_ok_true_with_trip(self):
        """ok=True + stop_condition_tripped=True is contradictory."""
        from clawpm.judges.stop_condition import JudgeVerdict
        with pytest.raises(ValueError):
            JudgeVerdict(ok=True, reason="done", stop_condition_tripped=True)

    def test_impossible_and_trip_together_raises(self):
        """impossible=True + stop_condition_tripped=True are mutually exclusive."""
        from clawpm.judges.stop_condition import JudgeVerdict
        with pytest.raises(ValueError):
            JudgeVerdict(ok=False, reason="x", impossible=True, stop_condition_tripped=True)

    def test_evaluate_returns_report_back_when_transcript_declares_trip(self):
        """evaluate_stop_condition surfaces stop_condition_tripped when the judge
        returns that field in its JSON output."""
        from clawpm.judges.stop_condition import evaluate_stop_condition

        def fake_judge(prompt: str) -> str:
            return '{"ok": false, "stop_condition_tripped": true, "reason": "STOP_CONDITION_TRIPPED: schema migration detected"}'

        verdict = evaluate_stop_condition(
            rubric="dummy rubric",
            transcript="Agent: I noticed this needs a schema migration. STOP_CONDITION_TRIPPED.",
            invoker=fake_judge,
        )
        assert not verdict.ok
        assert verdict.stop_condition_tripped
        assert not verdict.impossible

    def test_report_back_verdict_not_blocked_in_hook_output(self):
        """map_verdict_to_hook_output must NOT emit decision=block for a trip."""
        from clawpm.judges.stop_condition import JudgeVerdict, map_verdict_to_hook_output
        v = JudgeVerdict(ok=False, reason="STOP_CONDITION_TRIPPED: x", stop_condition_tripped=True)
        out = map_verdict_to_hook_output(v)
        assert out.get("decision") != "block"
        assert out.get("continue") is True


# ---------------------------------------------------------------------------
# C. Delegability: dispatch REFUSES human tasks
# ---------------------------------------------------------------------------

class TestDelegabilityField:
    def test_task_defaults_delegability_to_either(self, temp_portfolio):
        """Backward-compat: existing tasks without delegability default to either."""
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Legacy task")
        assert task is not None
        assert task.delegability == "either"

    def test_add_task_persists_delegability_agent(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Agent task", delegability="agent")
        assert task is not None
        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded is not None
        assert reloaded.delegability == "agent"

    def test_add_task_persists_delegability_human(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Human task", delegability="human")
        assert task is not None
        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded is not None
        assert reloaded.delegability == "human"

    def test_edit_task_persists_delegability(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Editable delegability task")
        assert task is not None
        updated = edit_task(config, "test-proj", task.id, delegability="human")
        assert updated is not None
        assert updated.delegability == "human"

    def test_to_dict_includes_delegability(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Dict delegability task", delegability="agent")
        assert task is not None
        d = task.to_dict()
        assert "delegability" in d
        assert d["delegability"] == "agent"

    def test_cli_add_delegability(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, [
            "tasks", "add",
            "--project", "test-proj",
            "--title", "CLI delegability task",
            "--delegability", "human",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert result.exit_code == 0, result.output
        config = load_portfolio_config(temp_portfolio)
        from clawpm.tasks import list_tasks
        tasks = list_tasks(config, "test-proj")
        assert len(tasks) == 1
        assert tasks[0].delegability == "human"

    def test_cli_edit_delegability(self, temp_portfolio):
        runner = CliRunner()
        runner.invoke(main, [
            "tasks", "add",
            "--project", "test-proj",
            "--title", "Edit delegability task",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        config = load_portfolio_config(temp_portfolio)
        from clawpm.tasks import list_tasks
        task_id = list_tasks(config, "test-proj")[0].id
        result = runner.invoke(main, [
            "tasks", "edit",
            "--project", "test-proj",
            task_id,
            "--delegability", "agent",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert result.exit_code == 0, result.output
        reloaded = get_task(config, "test-proj", task_id)
        assert reloaded is not None
        assert reloaded.delegability == "agent"

    def test_delegability_invalid_value_rejected_by_cli(self, temp_portfolio):
        """--delegability with an invalid value is rejected by CLI."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "tasks", "add",
            "--project", "test-proj",
            "--title", "Bad delegability task",
            "--delegability", "robot",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert result.exit_code != 0


class TestDelegabilityDispatchGate:
    """The dispatch path REFUSES to auto-dispatch a human task."""

    def test_dispatch_refuses_human_task_cli(self, temp_portfolio_with_repo):
        """tasks dispatch on a human-delegability task exits non-zero and
        does NOT write dispatch settings."""
        portfolio_root, proj_dir = temp_portfolio_with_repo
        config = load_portfolio_config(portfolio_root)
        task = add_task(config, "test-proj", "Human-only task", delegability="human")
        assert task is not None
        task_id = task.id
        target = proj_dir / "human_target"
        target.mkdir()

        runner = CliRunner()
        result = runner.invoke(main, [
            "tasks", "dispatch",
            "--project", "test-proj",
            task_id,
            "--target-dir", str(target),
        ], env={"CLAWPM_PORTFOLIO": str(portfolio_root)})
        # Must not succeed
        assert result.exit_code != 0
        # Must explain why
        assert "human" in result.output.lower()
        # Must NOT have written dispatch settings
        from clawpm.dispatch import settings_path
        assert not settings_path(target).exists()

    def test_dispatch_allows_agent_task(self, temp_portfolio_with_repo):
        """tasks dispatch on an agent-delegability task proceeds normally."""
        portfolio_root, proj_dir = temp_portfolio_with_repo
        config = load_portfolio_config(portfolio_root)
        task = add_task(config, "test-proj", "Agent task", delegability="agent")
        assert task is not None
        task_id = task.id
        target = proj_dir / "agent_target"
        target.mkdir()

        runner = CliRunner()
        result = runner.invoke(main, [
            "tasks", "dispatch",
            "--project", "test-proj",
            task_id,
            "--target-dir", str(target),
        ], env={"CLAWPM_PORTFOLIO": str(portfolio_root)})
        from clawpm.dispatch import settings_path
        assert settings_path(target).exists(), (
            f"dispatch settings not written for agent task; exit={result.exit_code} "
            f"output={result.output}"
        )

    def test_dispatch_allows_either_task(self, temp_portfolio_with_repo):
        """tasks dispatch on an either-delegability task proceeds normally."""
        portfolio_root, proj_dir = temp_portfolio_with_repo
        config = load_portfolio_config(portfolio_root)
        task = add_task(config, "test-proj", "Either task", delegability="either")
        assert task is not None
        task_id = task.id
        target = proj_dir / "either_target"
        target.mkdir()

        runner = CliRunner()
        result = runner.invoke(main, [
            "tasks", "dispatch",
            "--project", "test-proj",
            task_id,
            "--target-dir", str(target),
        ], env={"CLAWPM_PORTFOLIO": str(portfolio_root)})
        from clawpm.dispatch import settings_path
        assert settings_path(target).exists(), (
            f"dispatch settings not written for either task; exit={result.exit_code} "
            f"output={result.output}"
        )

    def test_agent_dispatch_refuses_human_task(self, temp_portfolio_with_repo):
        """dispatch_agent raises AgentDispatchError when delegability=human is passed explicitly."""
        from clawpm.agent import AgentDispatchError, dispatch_agent
        portfolio_root, proj_dir = temp_portfolio_with_repo
        config = load_portfolio_config(portfolio_root)

        def fake_judge(prompt: str) -> str:
            return '{"ok": true, "reason": "done"}'

        with pytest.raises(AgentDispatchError, match="human"):
            dispatch_agent(
                config,
                "test-proj",
                "Do the thing",
                success_criteria=["tests pass"],
                judge_invoker=fake_judge,
                delegability="human",
            )


