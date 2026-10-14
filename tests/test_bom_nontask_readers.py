"""CLAWP-146: the BOM class beyond task frontmatter (CLAWP-144).

Append-only JSONL logs and TOML config files written or touched by a Windows
editor start with a UTF-8 BOM. A line reader that decodes plain ``utf-8`` sees
``\\ufeff{...}`` on line one, ``json.loads`` rejects it, and the reader's
"skip malformed lines" arm silently drops the FIRST record. The TOML loaders
raised outright. Each reader below is fed REAL BOM bytes via ``write_bytes``
over a file the real writer produced (so the record shape is the
auto-generated one, not a hand-picked one).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.models import PortfolioConfig, ProjectSettings

BOM = b"\xef\xbb\xbf"


def _add_bom(path: Path, *, crlf: bool = False) -> None:
    """Prefix ``path`` with a BOM (and optionally convert LF to CRLF)."""
    raw = path.read_bytes()
    if crlf:
        raw = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    assert not raw.startswith(BOM)
    path.write_bytes(BOM + raw)


@pytest.fixture(params=[False, True], ids=["lf", "crlf"])
def crlf(request):
    return request.param


# --------------------------------------------------------------- JSONL logs


def test_inbox_first_message_survives_bom(tmp_path, crlf):
    from clawpm import inbox

    inbox.send_message(tmp_path, "agent-a", "first", from_agent="main")
    inbox.send_message(tmp_path, "agent-a", "second", from_agent="main")
    _add_bom(inbox._inbox_file(tmp_path, "agent-a"), crlf=crlf)
    msgs = inbox.read_inbox(tmp_path, "agent-a", unacked_only=False)
    assert [m["message"] for m in msgs] == ["first", "second"]


def test_lease_first_event_survives_bom(tmp_path, crlf):
    from clawpm import leases

    leases.grant_lease(
        tmp_path, "T-001", "proj", 600, leases.FallbackPolicy.REQUEUE
    )
    _add_bom(leases._registry_path(tmp_path), crlf=crlf)
    assert leases.get_lease(tmp_path, "T-001", "proj") is not None


def test_worklog_first_entry_survives_bom(isolated_portfolio, crlf):
    from clawpm import worklog
    from clawpm.models import WorkLogAction

    cfg = isolated_portfolio.config
    worklog.add_entry(
        cfg, "test", WorkLogAction.COMMIT, task="T-1", summary="one",
        commit_hash="abc123",
    )
    worklog.add_entry(cfg, "test", WorkLogAction.PROGRESS, task="T-1", summary="two")
    _add_bom(worklog.get_worklog_path(cfg), crlf=crlf)
    assert {e.summary for e in worklog.read_entries(cfg)} == {"one", "two"}
    assert worklog.get_logged_commit_hashes(cfg) == {"abc123"}


def test_id_reservation_first_record_survives_bom(tmp_path, crlf):
    from clawpm import id_reservations as idr

    idr.record_reservation(tmp_path, "CLAWP", 7, "CLAWP-007", "proj")
    _add_bom(idr._ledger_path(tmp_path), crlf=crlf)
    assert idr.reserved_high_water(tmp_path, "CLAWP") == 7


def test_reflection_iteration_events_survive_bom(tmp_path, crlf):
    from clawpm import reflect

    path = reflect.write_iteration_event(tmp_path, "T-9", "proj", False, "no")
    reflect.write_iteration_event(tmp_path, "T-9", "proj", True, "yes")
    _add_bom(path, crlf=crlf)
    assert reflect.count_iterations_for_task(tmp_path, "T-9", "proj") == 2


def test_reflection_done_event_survives_bom(tmp_path):
    from clawpm import reflect

    ref = tmp_path / "reflections" / "T-9.jsonl"
    ref.parent.mkdir()
    ref.write_text(
        json.dumps({"event": "task_done", "task_id": "T-9", "project_id": "p"})
        + "\n",
        encoding="utf-8",
    )
    _add_bom(ref)
    assert [r["task_id"] for r in reflect._iter_done_events(tmp_path, "p")] == ["T-9"]


def test_history_first_mention_survives_bom(tmp_path):
    from clawpm import history

    log = tmp_path / "chat.jsonl"
    log.write_text(
        json.dumps({"timestamp": "2026-10-01T00:00:00Z", "text": "worked on CLAWP-146"})
        + "\n",
        encoding="utf-8",
    )
    _add_bom(log)
    assert any(m.task_id == "CLAWP-146" for m in history.extract_task_mentions(log))


def test_dispatch_registry_first_event_survives_bom(tmp_path, crlf):
    from clawpm import dispatch

    target = tmp_path / "wt"
    target.mkdir()
    dispatch.register_dispatch(tmp_path, "T-1", "proj", target)
    _add_bom(dispatch._dispatch_registry_path(tmp_path), crlf=crlf)
    assert dispatch.active_dispatch_dirs(tmp_path, "T-1", "proj") == [target]


def test_session_registry_first_event_survives_bom(tmp_path, crlf):
    from clawpm import sessions

    wt = tmp_path / "wt"
    wt.mkdir()
    sessions.register_session(tmp_path, "sess-1", "T-1", "proj", wt)
    _add_bom(sessions._registry_path(tmp_path), crlf=crlf)
    assert [s.session_id for s in sessions.active_sessions(tmp_path)] == ["sess-1"]


def test_issues_list_first_issue_survives_bom(isolated_portfolio):
    issues = isolated_portfolio.project_dir / ".agent" / "issues.jsonl"
    issues.parent.mkdir()
    issues.write_text(
        json.dumps({"type": "bug", "severity": "low", "summary": "first"}) + "\n",
        encoding="utf-8",
    )
    _add_bom(issues)
    result = CliRunner().invoke(
        main, ["--format", "json", "issues", "list", "--project", "test"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["count"] == 1


# ------------------------------------------------------------------- TOML


def test_project_settings_toml_tolerates_bom(tmp_path, crlf):
    p = tmp_path / ".project" / "settings.toml"
    p.parent.mkdir()
    p.write_text('id = "bom"\nname = "Bom"\npriority = 2\n', encoding="utf-8")
    _add_bom(p, crlf=crlf)
    s = ProjectSettings.load(p)
    assert (s.id, s.name, s.priority) == ("bom", "Bom", 2)


def test_portfolio_toml_tolerates_bom(tmp_path, crlf):
    p = tmp_path / "portfolio.toml"
    p.write_text(
        f'portfolio_root = "{tmp_path.as_posix()}"\nproject_roots = []\n',
        encoding="utf-8",
    )
    _add_bom(p, crlf=crlf)
    assert PortfolioConfig.load(p).portfolio_root == tmp_path


def test_project_id_regex_fallback_tolerates_bom(tmp_path):
    """The regex fallback (TOML unparseable, e.g. backslash repo_path) anchors
    on ``^id``; a BOM on line one must not hide it."""
    from clawpm.discovery import _read_project_id_from_settings

    p = tmp_path / "settings.toml"
    p.write_text('id = "fallbk"\nrepo_path = "C:\\bad\\path"\n', encoding="utf-8")
    _add_bom(p)
    assert _read_project_id_from_settings(p) == "fallbk"


# ----------------------------------------------------- other text readers


def test_patterns_file_first_line_survives_bom(tmp_path):
    from clawpm.cli.base import _read_patterns_file

    p = tmp_path / "scope.txt"
    p.write_bytes(BOM + b"src/**\ndocs/**\n")
    assert _read_patterns_file(str(p), "--scope-file", None) == ["src/**", "docs/**"]


# ------------------------------------------------------------- edit_fog


def test_edit_fog_on_frontmatterless_file_keeps_heading(isolated_portfolio, crlf):
    """Absent frontmatter: the synthesised closing fence used to be fused to
    the heading (``---# Real task``), hiding the title."""
    from clawpm.tasks import edit_fog, get_task

    pf = isolated_portfolio
    f = pf.tasks_dir / "FOG-001.md"
    nl = b"\r\n" if crlf else b"\n"
    f.write_bytes(b"# Real task" + nl + nl + b"Body text." + nl)
    task = edit_fog(pf.config, pf.project_id, "FOG-001", add="pricing unknown")
    assert task is not None
    text = f.read_bytes().decode("utf-8")
    assert "---# " not in text
    assert "\n---\n" in text.replace("\r\n", "\n")
    assert "# Real task" in text
    assert get_task(pf.config, pf.project_id, "FOG-001").title == "Real task"


def test_edit_fog_on_bom_frontmatterless_file_keeps_heading(isolated_portfolio):
    from clawpm.tasks import edit_fog, get_task

    pf = isolated_portfolio
    f = pf.tasks_dir / "FOG-002.md"
    f.write_bytes(BOM + b"# Real task\n\nBody text.\n")
    edit_fog(pf.config, pf.project_id, "FOG-002", add="x")
    raw = f.read_bytes()
    assert raw.count(BOM) == 1 and raw.startswith(BOM)
    assert b"---# " not in raw
    assert get_task(pf.config, pf.project_id, "FOG-002").title == "Real task"


# ------------------------------------------- round 2: second-audit readers


def test_doctor_work_log_first_record_survives_bom(isolated_portfolio):
    """cli/project.py doctor: a BOM-prefixed FIRST work_log record used to be
    dropped by the skip-malformed arm, so a task whose newest touch was that
    record was reported stale."""
    from datetime import datetime, timedelta, timezone

    pf = isolated_portfolio
    old = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    prog = pf.tasks_dir / "test-001.progress.md"
    prog.write_text(
        f"---\nid: test-001\nupdated: {old}\n---\n# Prog\n", encoding="utf-8"
    )
    log = pf.root / "work_log.jsonl"
    fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    log.write_text(
        json.dumps({"task": "test-001", "ts": fresh, "action": "progress"}) + "\n",
        encoding="utf-8",
    )
    _add_bom(log)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    stale_ids = [s["task_id"] for s in json.loads(result.output).get("stale_tasks", [])]
    assert "test-001" not in stale_ids


def test_tasks_add_stdin_body_tolerates_bom(isolated_portfolio):
    from clawpm.tasks import get_task

    pf = isolated_portfolio
    result = CliRunner().invoke(
        main,
        ["--project", "test", "tasks", "add", "--title", "T", "--stdin"],
        input="\ufeff## Plan\n\nbody\n",
    )
    assert result.exit_code == 0, result.output
    task_id = json.loads(result.output)["data"]["id"]
    text = get_task(pf.config, "test", task_id).file_path.read_bytes()
    assert BOM not in text


def test_emit_tree_stdin_tolerates_bom(isolated_portfolio):
    result = CliRunner().invoke(
        main,
        ["--project", "test", "tasks", "emit-tree", "--dry-run"],
        input="\ufeff" + json.dumps({
            "schema_version": 1,
            "root": {"title": "R"},
            "leaves": [{"ref": "L1", "parent_ref": None, "title": "A",
                        "leaf_key": "k1"}],
        }),
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "ok"
