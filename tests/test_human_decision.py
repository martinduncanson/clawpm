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
