"""Tests for agent_profile — capability/skill hint on tasks (CLAWP-038).

Coverage:
1. add_task(agent_profile=...) round-trips through from_file / to_dict.
2. Legacy task files with no agent_profile field load with None (back-compat).
3. add_subtask(agent_profile=...) round-trips.
4. dispatch_agent(agent_profile=...) records the profile in the reflection
   event on the DONE path and in the iteration event on the BLOCKED path.
5. The dispatch result dict surfaces agent_profile.

Profile routing to a concrete subagent_type is intentionally out of scope:
clawpm's dispatch shells out to a `claude --print`-style invoker, so the
load-bearing behaviour for calibration is that the profile is RECORDED on the
task and in the reflection/iteration events. CLAWP-040 consumes it.
"""

from __future__ import annotations

import json
from pathlib import Path

from clawpm.agent import dispatch_agent
from clawpm.models import Task, TaskState
from clawpm.tasks import add_subtask, add_task, get_task

from test_agent_dispatch import (  # noqa: F401 — reuse fixtures/helpers
    _make_stub_invoker,
    temp_portfolio_with_repo,
)


def _read_events(root: Path, task_id: str) -> list[dict]:
    p = Path(root) / "reflections" / f"{task_id}.jsonl"
    if not p.exists():
        return []
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestTaskSerialization:
    def test_add_task_round_trips_agent_profile(self, temp_portfolio_with_repo):
        config = temp_portfolio_with_repo["config"]
        task = add_task(
            config, "test", title="Profiled", agent_profile="code-architect",
        )
        assert task is not None
        assert task.agent_profile == "code-architect"
        # Reloaded from disk
        reloaded = get_task(config, "test", task.id)
        assert reloaded.agent_profile == "code-architect"
        assert reloaded.to_dict()["agent_profile"] == "code-architect"

    def test_legacy_task_without_agent_profile_loads_as_none(
        self, temp_portfolio_with_repo
    ):
        """A task file written before CLAWP-038 (no agent_profile key) must
        load cleanly with agent_profile=None — no parse error, no crash."""
        tasks_dir = temp_portfolio_with_repo["tasks_dir"]
        legacy = tasks_dir / "TEST-900.md"
        legacy.write_text(
            "---\n"
            "id: TEST-900\n"
            "priority: 5\n"
            "created: 2026-01-01\n"
            "---\n"
            "# Legacy task\n\nbody\n",
            encoding="utf-8",
        )
        task = Task.from_file(legacy)
        assert task.agent_profile is None
        assert task.to_dict()["agent_profile"] is None

    def test_blank_agent_profile_normalises_to_none(
        self, temp_portfolio_with_repo
    ):
        tasks_dir = temp_portfolio_with_repo["tasks_dir"]
        f = tasks_dir / "TEST-901.md"
        f.write_text(
            "---\nid: TEST-901\nagent_profile: '   '\n---\n# t\n",
            encoding="utf-8",
        )
        assert Task.from_file(f).agent_profile is None

    def test_add_subtask_round_trips_agent_profile(
        self, temp_portfolio_with_repo
    ):
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="Parent")
        sub = add_subtask(
            config, "test", parent.id, "Child", agent_profile="code-reviewer",
        )
        assert sub is not None
        assert sub.agent_profile == "code-reviewer"
        assert get_task(config, "test", sub.id).agent_profile == "code-reviewer"


class TestDispatchRecordsProfile:
    def test_done_path_records_profile_in_reflection_event(
        self, temp_portfolio_with_repo
    ):
        config = temp_portfolio_with_repo["config"]
        stub = _make_stub_invoker('{"ok": true, "reason": "looks done"}')
        result = dispatch_agent(
            config=config,
            project_id="test",
            prompt="Do the thing",
            success_criteria=["the thing is done"],
            judge_invoker=stub,
            agent_profile="code-architect",
            init_codegraph=False,
        )
        assert result["agent_profile"] == "code-architect"
        subtask = get_task(config, "test", result["subtask_id"])
        assert subtask.state == TaskState.DONE

        events = _read_events(temp_portfolio_with_repo["root"], result["subtask_id"])
        done = [e for e in events if e.get("event") == "agent_dispatch_done"]
        assert done, "expected an agent_dispatch_done reflection event"
        assert done[0]["agent_profile"] == "code-architect"

    def test_blocked_path_records_profile_in_iteration_event(
        self, temp_portfolio_with_repo
    ):
        config = temp_portfolio_with_repo["config"]
        stub = _make_stub_invoker('{"ok": false, "reason": "not yet"}')
        result = dispatch_agent(
            config=config,
            project_id="test",
            prompt="Do the thing",
            success_criteria=["the thing is done"],
            judge_invoker=stub,
            agent_profile="security-reviewer",
            init_codegraph=False,
        )
        subtask = get_task(config, "test", result["subtask_id"])
        assert subtask.state == TaskState.BLOCKED

        events = _read_events(temp_portfolio_with_repo["root"], result["subtask_id"])
        iters = [e for e in events if e.get("event") == "iteration_event"]
        assert iters, "expected an iteration_event"
        assert iters[0]["agent_profile"] == "security-reviewer"

    def test_absent_profile_is_none_in_event(self, temp_portfolio_with_repo):
        """Back-compat: dispatch without a profile records agent_profile=None."""
        config = temp_portfolio_with_repo["config"]
        stub = _make_stub_invoker('{"ok": true, "reason": "done"}')
        result = dispatch_agent(
            config=config,
            project_id="test",
            prompt="x",
            success_criteria=["done"],
            judge_invoker=stub,
            init_codegraph=False,
        )
        assert result["agent_profile"] is None
        events = _read_events(temp_portfolio_with_repo["root"], result["subtask_id"])
        done = [e for e in events if e.get("event") == "agent_dispatch_done"]
        assert done and done[0]["agent_profile"] is None
