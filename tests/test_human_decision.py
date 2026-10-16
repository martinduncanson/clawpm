"""Tests for CLAWP-154 — awaiting-human decision record on a blocked task.

Covers (a)-(e) of the brief:
  a. ``block --question/--option/--recommend`` writes frontmatter + worklog.
  b. ``tasks list --needs-human --all-projects`` lists only blocked-on-human
     tasks across projects, with a stable JSON row shape.
  c. ``unblock --answer`` records the answer, clears the block via the
     existing path, and keeps question/options.
  d. A plain blocked task (no question) is not listed.
  e. Task files without the new fields round-trip byte-identically.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.models import TaskState
from clawpm.tasks import add_task, change_task_state, get_task
from clawpm.worklog import read_entries
from tests.test_all_projects import _env, _make_portfolio


@pytest.fixture
def port(tmp_path, monkeypatch):
    return _make_portfolio(tmp_path, monkeypatch, [
        {"id": "alpha", "priority": 1, "prefix": "ALPHA"},
        {"id": "beta", "priority": 5, "prefix": "BETA"},
    ])


def _block_with_question(port, project, task_id, *extra):
    return CliRunner().invoke(
        main,
        [
            "block", "-p", project, task_id,
            "--question", "Ship the migration this week?",
            "--option", "Ship now", "--option", "Wait for review",
            "--recommend", "Wait for review",
            *extra,
        ],
        env=_env(port),
    )


def test_block_with_question_writes_frontmatter_and_worklog(port):
    t = add_task(port.config, "alpha", "Alpha decision")
    r = _block_with_question(port, "alpha", t.id)
    assert r.exit_code == 0, r.output

    task = get_task(port.config, "alpha", t.id)
    assert task.state == TaskState.BLOCKED
    assert task.blocked_on == "human"
    assert task.question == "Ship the migration this week?"
    assert [o["label"] for o in task.options] == ["Ship now", "Wait for review"]
    assert task.recommendation == "Wait for review"
    assert task.answer is None

    entries = [e for e in read_entries(port.config, project="alpha") if e.task == t.id]
    blocked = [e for e in entries if e.action.value == "blocked"]
    assert blocked and "Ship the migration this week?" in blocked[-1].summary
    assert "Wait for review" in blocked[-1].summary


def test_recommendation_must_match_an_option(port):
    t = add_task(port.config, "alpha", "Alpha decision")
    r = CliRunner().invoke(
        main,
        ["block", "-p", "alpha", t.id, "--question", "Q?", "--option", "A",
         "--recommend", "Z"],
        env=_env(port),
    )
    assert r.exit_code != 0
    # Refused before any move: the task is still open.
    assert get_task(port.config, "alpha", t.id).state == TaskState.OPEN


def test_needs_human_all_projects_only_blocked_on_human(port):
    a = add_task(port.config, "alpha", "Alpha decision")
    b = add_task(port.config, "beta", "Beta decision")
    plain = add_task(port.config, "beta", "Plain blocked")
    add_task(port.config, "alpha", "Just open")
    assert _block_with_question(port, "alpha", a.id).exit_code == 0
    assert _block_with_question(port, "beta", b.id).exit_code == 0
    assert CliRunner().invoke(
        main, ["block", "-p", "beta", plain.id, "--note", "waiting on vendor"],
        env=_env(port),
    ).exit_code == 0

    r = CliRunner().invoke(
        main, ["tasks", "list", "--needs-human", "--all-projects"], env=_env(port)
    )
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    assert [(row["project"], row["id"]) for row in rows] == [
        ("alpha", a.id), ("beta", b.id),
    ]
    row = rows[0]
    assert set(row) == {
        "id", "project", "title", "question", "options", "recommendation",
        "blocked_since",
    }
    assert row["question"] == "Ship the migration this week?"
    assert row["options"] == [{"label": "Ship now"}, {"label": "Wait for review"}]
    assert row["recommendation"] == "Wait for review"
    assert row["blocked_since"]


def test_plain_blocked_task_not_listed(port):
    plain = add_task(port.config, "alpha", "Plain blocked")
    assert CliRunner().invoke(
        main, ["block", "-p", "alpha", plain.id, "--note", "waiting"],
        env=_env(port),
    ).exit_code == 0
    r = CliRunner().invoke(
        main, ["tasks", "list", "--needs-human", "--all-projects"], env=_env(port)
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == []


def test_unblock_answer_records_and_keeps_question(port):
    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0

    r = CliRunner().invoke(
        main, ["unblock", "-p", "alpha", t.id, "--answer", "Wait for review"],
        env=_env(port),
    )
    assert r.exit_code == 0, r.output

    task = get_task(port.config, "alpha", t.id)
    assert task.state == TaskState.OPEN
    assert task.answer == "Wait for review"
    assert task.question == "Ship the migration this week?"
    assert [o["label"] for o in task.options] == ["Ship now", "Wait for review"]

    entries = [e for e in read_entries(port.config, project="alpha") if e.task == t.id]
    unblocks = [e for e in entries if e.action.value == "unblock"]
    assert unblocks and "Wait for review" in unblocks[-1].summary

    listed = CliRunner().invoke(
        main, ["tasks", "list", "--needs-human", "--all-projects"], env=_env(port)
    )
    assert json.loads(listed.output) == []


def test_answered_but_still_blocked_not_listed(port):
    from clawpm.tasks import record_human_answer

    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0
    record_human_answer(port.config, "alpha", t.id, "Ship now")
    r = CliRunner().invoke(
        main, ["tasks", "list", "--needs-human", "--all-projects"], env=_env(port)
    )
    assert json.loads(r.output) == []


def test_files_without_new_fields_round_trip_byte_identically(port):
    t = add_task(port.config, "alpha", "No decision here")
    path = get_task(port.config, "alpha", t.id).file_path
    before = path.read_bytes()
    task = get_task(port.config, "alpha", t.id)
    assert task.blocked_on is None and task.question is None
    assert task.options == [] and task.recommendation is None and task.answer is None
    d = task.to_dict()
    for key in ("blocked_on", "question", "options", "recommendation", "answer"):
        assert key not in d
    # Read-only path leaves bytes alone; a plain block/unblock cycle adds no
    # decision keys either.
    assert path.read_bytes() == before
    change_task_state(port.config, "alpha", t.id, TaskState.BLOCKED, note="x")
    change_task_state(port.config, "alpha", t.id, TaskState.OPEN, note="y")
    after = get_task(port.config, "alpha", t.id).file_path.read_text(encoding="utf-8")
    for key in ("blocked_on:", "question:", "recommendation:", "answer:"):
        assert key not in after


def test_question_refused_on_unblocked_task(port):
    from clawpm.tasks import record_human_question

    t = add_task(port.config, "alpha", "Open task")
    with pytest.raises(ValueError):
        record_human_question(port.config, "alpha", t.id, "Q?", [{"label": "A"}])


def _needs_human(port):
    r = CliRunner().invoke(
        main, ["tasks", "list", "--needs-human", "--all-projects"], env=_env(port)
    )
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def test_unblock_without_answer_then_plain_reblock_not_listed(port):
    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0
    assert CliRunner().invoke(
        main, ["unblock", "-p", "alpha", t.id], env=_env(port)
    ).exit_code == 0
    assert CliRunner().invoke(
        main, ["block", "-p", "alpha", t.id, "--note", "waiting on vendor"],
        env=_env(port),
    ).exit_code == 0

    task = get_task(port.config, "alpha", t.id)
    assert task.state == TaskState.BLOCKED
    assert task.question is None and task.options == []
    assert task.recommendation is None and task.answer is None
    assert _needs_human(port) == []
    # The old record survives in the worklog history.
    entries = [e for e in read_entries(port.config, project="alpha") if e.task == t.id]
    assert any("Ship the migration this week?" in e.summary for e in entries)


def test_state_open_then_plain_reblock_not_listed(port):
    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0
    change_task_state(port.config, "alpha", t.id, TaskState.OPEN, note="reopened")
    assert CliRunner().invoke(
        main, ["block", "-p", "alpha", t.id, "--note", "again"], env=_env(port)
    ).exit_code == 0
    assert get_task(port.config, "alpha", t.id).question is None
    assert _needs_human(port) == []


def test_answered_task_reblocked_with_new_question_replaces_record(port):
    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0
    assert CliRunner().invoke(
        main, ["unblock", "-p", "alpha", t.id, "--answer", "Ship now"], env=_env(port)
    ).exit_code == 0
    r = CliRunner().invoke(
        main,
        ["block", "-p", "alpha", t.id, "--question", "Second question?",
         "--option", "Yes", "--option", "No", "--recommend", "No"],
        env=_env(port),
    )
    assert r.exit_code == 0, r.output
    task = get_task(port.config, "alpha", t.id)
    assert task.question == "Second question?"
    assert [o["label"] for o in task.options] == ["Yes", "No"]
    assert task.recommendation == "No" and task.answer is None
    assert [row["id"] for row in _needs_human(port)] == [t.id]


# CLAWP-156 — the record is dropped on ENTERING blocked from another state, in
# change_task_state itself, so `tasks state` cannot bypass the block shortcut.
# (Clearing on LEAVING would erase the question `unblock --answer` keeps.)

def test_state_open_then_state_blocked_not_listed(port):
    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0
    for new_state in ("open", "blocked"):
        r = CliRunner().invoke(
            main, ["tasks", "state", "-p", "alpha", t.id, new_state], env=_env(port)
        )
        assert r.exit_code == 0, r.output
    task = get_task(port.config, "alpha", t.id)
    assert task.state == TaskState.BLOCKED
    assert task.blocked_on is None and task.question is None
    assert task.options == [] and task.recommendation is None and task.answer is None
    assert _needs_human(port) == []


@pytest.mark.parametrize("via", [TaskState.OPEN, TaskState.PROGRESS, TaskState.DONE])
def test_every_edge_into_blocked_clears_the_record(port, via):
    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0
    change_task_state(port.config, "alpha", t.id, via, force=True)
    # Leaving blocked keeps the record (unblock --answer relies on it).
    assert get_task(port.config, "alpha", t.id).question is not None
    change_task_state(port.config, "alpha", t.id, TaskState.BLOCKED)
    task = get_task(port.config, "alpha", t.id)
    assert task.state == TaskState.BLOCKED and task.question is None
    assert _needs_human(port) == []


def test_answered_record_cleared_on_state_reblock(port):
    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0
    assert CliRunner().invoke(
        main, ["unblock", "-p", "alpha", t.id, "--answer", "Ship now"], env=_env(port)
    ).exit_code == 0
    change_task_state(port.config, "alpha", t.id, TaskState.BLOCKED)
    task = get_task(port.config, "alpha", t.id)
    assert task.question is None and task.answer is None


def test_staying_blocked_keeps_the_live_question(port):
    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0
    change_task_state(port.config, "alpha", t.id, TaskState.BLOCKED, note="again")
    assert get_task(port.config, "alpha", t.id).question == "Ship the migration this week?"
    assert [row["id"] for row in _needs_human(port)] == [t.id]


def test_plain_task_state_round_trip_is_byte_identical(port):
    t = add_task(port.config, "alpha", "No decision here")
    change_task_state(port.config, "alpha", t.id, TaskState.BLOCKED)
    path = get_task(port.config, "alpha", t.id).file_path
    before = path.read_bytes()
    change_task_state(port.config, "alpha", t.id, TaskState.BLOCKED)  # no-op
    assert path.read_bytes() == before
    # open -> blocked on a never-questioned task writes no decision keys and
    # touches nothing beyond the existing `updated` stamp.
    change_task_state(port.config, "alpha", t.id, TaskState.OPEN)
    change_task_state(port.config, "alpha", t.id, TaskState.BLOCKED)
    after = get_task(port.config, "alpha", t.id).file_path.read_bytes()
    assert after.replace(b"\r\n", b"\n") == before.replace(b"\r\n", b"\n")
