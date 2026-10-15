"""PR #122 gate r1 fix round (CLAWP-154): the decision-record writer and the
``block`` / ``unblock`` shortcut ordering.

  1. Every write uses its own temp file (no shared fixed temp name).
  2. ``block`` transitions FIRST and clears a stale record afterwards; a
     failed transition keeps an open task's question, a failed clear is loud.
  3. The writer splices only the decision keys (plus ``updated``), keeping
     comments, key order, quoting and line endings; structures it cannot
     splice safely are refused with the file byte-identical.
"""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.models import TaskState
from clawpm.tasks import add_task, change_task_state, get_task
from tests.test_all_projects import _env, _make_portfolio
from tests.test_human_decision import _block_with_question


@pytest.fixture
def port(tmp_path, monkeypatch):
    return _make_portfolio(tmp_path, monkeypatch, [
        {"id": "alpha", "priority": 1, "prefix": "ALPHA"},
    ])


def _open_with_stale_record(port):
    """A task that is OPEN but still carries a decision record."""
    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0
    change_task_state(port.config, "alpha", t.id, TaskState.OPEN, note="reopened")
    task = get_task(port.config, "alpha", t.id)
    assert task.state == TaskState.OPEN and task.question
    return t


def test_plain_block_transition_failure_keeps_open_tasks_record(port, monkeypatch):
    t = _open_with_stale_record(port)

    import clawpm.services.tasks as svc

    monkeypatch.setattr(
        svc, "transition",
        lambda config, **kw: {"ok": False, "task_id": kw["task_id"],
                              "error": "boom", "message": "transition refused"},
    )
    r = CliRunner().invoke(
        main, ["block", "-p", "alpha", t.id, "--note", "x"], env=_env(port)
    )
    assert r.exit_code != 0
    task = get_task(port.config, "alpha", t.id)
    assert task.state == TaskState.OPEN
    assert task.question == "Ship the migration this week?"
    assert [o["label"] for o in task.options] == ["Ship now", "Wait for review"]
    assert task.recommendation == "Wait for review"


def test_plain_block_clear_failure_is_loud(port, monkeypatch):
    t = _open_with_stale_record(port)

    import clawpm.tasks as tasks_mod

    def _boom(*a, **k):
        raise OSError("disk says no")

    monkeypatch.setattr(tasks_mod, "_rewrite_decision_frontmatter", _boom)
    r = CliRunner().invoke(
        main, ["block", "-p", "alpha", t.id, "--note", "x"], env=_env(port)
    )
    assert r.exit_code != 0
    assert "stale" in r.output.lower()
    # The block itself landed; the error says the record is still there.
    assert get_task(port.config, "alpha", t.id).state == TaskState.BLOCKED


def test_unblock_without_answer_clear_failure_is_loud(port, monkeypatch):
    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0

    import clawpm.tasks as tasks_mod

    def _boom(*a, **k):
        raise OSError("disk says no")

    monkeypatch.setattr(tasks_mod, "_rewrite_decision_frontmatter", _boom)
    r = CliRunner().invoke(main, ["unblock", "-p", "alpha", t.id], env=_env(port))
    assert r.exit_code != 0
    assert "stale" in r.output.lower()
    assert get_task(port.config, "alpha", t.id).state == TaskState.OPEN


def test_clear_with_snapshot_skips_a_record_changed_meanwhile(port):
    from clawpm.tasks import (
        clear_stale_human_decision, record_human_question, stale_human_decision,
    )

    t = _open_with_stale_record(port)
    snap = stale_human_decision(get_task(port.config, "alpha", t.id))
    assert snap is not None
    # Another session re-blocks with a NEW live question before the clear runs.
    change_task_state(port.config, "alpha", t.id, TaskState.BLOCKED, note="again")
    record_human_question(port.config, "alpha", t.id, "A different question?", ["X"])
    clear_stale_human_decision(port.config, "alpha", t.id, snapshot=snap)
    assert get_task(port.config, "alpha", t.id).question == "A different question?"


def test_concurrent_writers_do_not_share_a_temp_name(port, monkeypatch):
    """A second writer finishing between our temp write and our rename must not
    break ours (a fixed temp name made the outer rename fail or lose data)."""
    import clawpm.tasks as tasks_mod
    from clawpm.tasks import record_human_answer

    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0
    real = tasks_mod.retry_transient
    state = {"nested": False}

    def spy(fn, *a, **k):
        if getattr(fn, "__name__", "") == "replace" and not state["nested"]:
            state["nested"] = True
            record_human_answer(port.config, "alpha", t.id, "inner")
        return real(fn, *a, **k)

    monkeypatch.setattr(tasks_mod, "retry_transient", spy)
    record_human_answer(port.config, "alpha", t.id, "outer")
    assert state["nested"]
    task = get_task(port.config, "alpha", t.id)
    assert task.answer == "outer"
    assert not [p for p in os.listdir(task.file_path.parent) if p.endswith(".tmp")]


def test_failed_rename_removes_its_temp_and_leaves_file(port, monkeypatch):
    from clawpm.tasks import record_human_answer

    t = add_task(port.config, "alpha", "Alpha decision")
    assert _block_with_question(port, "alpha", t.id).exit_code == 0
    path = get_task(port.config, "alpha", t.id).file_path
    before = path.read_bytes()

    def _boom(src, dst):
        raise OSError("rename refused")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        record_human_answer(port.config, "alpha", t.id, "A")
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert not [p for p in os.listdir(path.parent) if p.endswith(".tmp")]


def _crlf_commented_task(port):
    """A blocked task file with comments, quoting, trailing comment and CRLF."""
    t = add_task(port.config, "alpha", "Alpha decision")
    change_task_state(port.config, "alpha", t.id, TaskState.BLOCKED, note="b")
    path = get_task(port.config, "alpha", t.id).file_path
    text = path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    head, fence, rest = text.partition("\n---\n")
    head = head.replace("---\n", "---\n# keep: operator comment\n", 1)
    head = head.replace(f"id: {t.id}", f"id: '{t.id}'  # quoted id", 1)
    assert "# quoted id" in head
    new = head + "\n# trailing fm comment" + fence + rest
    path.write_bytes(new.replace("\n", "\r\n").encode("utf-8"))
    return t, path


def _split(raw: bytes):
    lines = raw.decode("utf-8").split("\r\n")
    close = next(i for i in range(1, len(lines)) if lines[i] == "---")
    return lines, close


def test_record_question_preserves_everything_outside_decision_keys(port):
    from clawpm.tasks import record_human_question

    t, path = _crlf_commented_task(port)
    before = path.read_bytes()
    b_lines, b_close = _split(before)
    record_human_question(
        port.config, "alpha", t.id, "Ship it?",
        ["Yes", {"label": "No", "detail": "later"}], "No",
    )
    after = path.read_bytes()
    assert after.count(b"\n") == after.count(b"\r\n")  # no bare LF introduced
    a_lines, a_close = _split(after)

    def keep(ls):
        return [ln for ln in ls if not ln.startswith("updated:")]

    # Every original frontmatter line (bar the `updated` stamp) survives in
    # order, with only the new decision lines inserted before the fence.
    assert keep(a_lines[:b_close]) == keep(b_lines[:b_close])
    assert a_lines[a_close:] == b_lines[b_close:]  # fence + body byte-identical
    joined = "\r\n".join(a_lines)
    assert "# keep: operator comment" in joined
    assert f"id: '{t.id}'  # quoted id" in joined
    assert "# trailing fm comment" in joined
    task = get_task(port.config, "alpha", t.id)
    assert task.question == "Ship it?" and task.recommendation == "No"
    assert task.options == [{"label": "Yes"}, {"label": "No", "detail": "later"}]


def test_answer_and_clear_keep_comments_and_quoting(port):
    from clawpm.tasks import (
        clear_stale_human_decision, record_human_answer, record_human_question,
    )

    t, path = _crlf_commented_task(port)
    record_human_question(port.config, "alpha", t.id, "Ship it?", ["Yes", "No"], "Yes")
    raw = path.read_bytes().decode("utf-8")
    assert "question: Ship it?" in raw
    raw = raw.replace("question: Ship it?", 'question: "Ship it?"  # why we ask', 1)
    path.write_bytes(raw.encode("utf-8"))

    record_human_answer(port.config, "alpha", t.id, "Yes please")
    mid = path.read_bytes().decode("utf-8")
    assert 'question: "Ship it?"  # why we ask' in mid
    assert "# keep: operator comment" in mid and "answer: Yes please" in mid

    change_task_state(port.config, "alpha", t.id, TaskState.OPEN, note="o")
    clear_stale_human_decision(port.config, "alpha", t.id)
    end = path.read_bytes().decode("utf-8")
    for gone in ("question:", "answer:", "options:", "recommendation:", "blocked_on:"):
        assert gone not in end
    assert "# keep: operator comment" in end and "# trailing fm comment" in end
    assert f"id: '{t.id}'  # quoted id" in end
    assert end.count("\n") == end.count("\r\n")


def test_unspliceable_frontmatter_is_refused_and_left_byte_identical(port):
    from clawpm.tasks import record_human_question

    t = add_task(port.config, "alpha", "Alpha decision")
    change_task_state(port.config, "alpha", t.id, TaskState.BLOCKED, note="b")
    path = get_task(port.config, "alpha", t.id).file_path
    text = path.read_bytes().decode("utf-8")
    # A duplicate top-level key parses (last wins) but cannot be spliced safely.
    text = text.replace("\n---\n", "\ntitle: dup\n---\n", 1)
    path.write_bytes(text.encode("utf-8"))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        record_human_question(port.config, "alpha", t.id, "Q?", ["A"])
    assert path.read_bytes() == before
