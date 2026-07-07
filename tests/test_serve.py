"""Tests for the ClawPM web layer (CLAWP-078).

The web layer is a read-only dashboard. These tests exercise every route via
FastAPI's TestClient — the happy path AND the error paths — and assert the two
contract invariants the demotion established:

  1. Every error response uses the single envelope ``{"error": {code, message}}``
     with a proper HTTP status (400/404/405).
  2. Every mutating route returns 405 and performs no write (no task files
     created, no ``.agent/issues.jsonl`` written, settings.toml untouched).

Plus the CLI graceful-degradation path: ``clawpm serve`` without the optional
``web`` extra installed exits 1 with an install hint (tested by forcing the
import to fail, so it runs even with fastapi present).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from clawpm.discovery import load_portfolio_config
from clawpm.models import TaskComplexity, TaskState, WorkLogAction
from clawpm.serve import create_app
from clawpm.tasks import add_task, change_task_state
from clawpm.worklog import add_entry


@pytest.fixture
def portfolio(monkeypatch):
    """A portfolio with one project holding open / progress / blocked tasks and
    a work-log entry, built through the real core functions so the on-disk
    shape matches what the CLI produces.
    """
    temp_dir = tempfile.mkdtemp(prefix="clawpm_serve_test_")
    root = Path(temp_dir)
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{root.as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )
    repo = root / "demo"
    project_meta = repo / ".project"
    project_meta.mkdir(parents=True)
    settings_path = project_meta / "settings.toml"
    settings_path.write_text(
        'id = "demo"\nname = "Demo"\nstatus = "active"\npriority = 3\n'
        f'repo_path = "{repo.as_posix()}"\n',
        encoding="utf-8",
    )
    (project_meta / "tasks").mkdir()

    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
    config = load_portfolio_config(root)

    t_open = add_task(config, "demo", "Open task", complexity=TaskComplexity.S)
    t_prog = add_task(config, "demo", "Progress task", complexity=TaskComplexity.M)
    t_block = add_task(config, "demo", "Blocked task", complexity=TaskComplexity.L)
    change_task_state(config, "demo", t_prog.id, TaskState.PROGRESS)
    change_task_state(config, "demo", t_block.id, TaskState.BLOCKED)
    add_entry(config, project="demo", action=WorkLogAction.NOTE, summary="did a thing")

    yield {
        "root": root,
        "repo": repo,
        "settings_path": settings_path,
        "config": config,
        "open_id": t_open.id,
        "progress_id": t_prog.id,
        "blocked_id": t_block.id,
    }


@pytest.fixture
def client(portfolio):
    return TestClient(create_app())


def _assert_error_envelope(resp, status, code=None):
    assert resp.status_code == status
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert isinstance(body["error"]["code"], str)
    assert isinstance(body["error"]["message"], str)
    if code is not None:
        assert body["error"]["code"] == code


# ---------------------------------------------------------------- reads ----

def test_index_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ClawPM" in resp.text
    assert "text/html" in resp.headers["content-type"]


def test_projects_list(client):
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    ids = [p["id"] for p in resp.json()]
    assert "demo" in ids


def test_project_context_found(client):
    resp = client.get("/api/projects/demo")
    assert resp.status_code == 200
    assert resp.json()["id"] == "demo"


def test_project_context_missing_is_404(client):
    resp = client.get("/api/projects/nope")
    _assert_error_envelope(resp, 404, "not_found")


def test_project_tasks_list(client, portfolio):
    resp = client.get("/api/projects/demo/tasks")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert portfolio["open_id"] in ids


def test_project_tasks_state_filter(client, portfolio):
    resp = client.get("/api/projects/demo/tasks", params={"state": "blocked"})
    assert resp.status_code == 200
    tasks = resp.json()
    assert [t["id"] for t in tasks] == [portfolio["blocked_id"]]


def test_project_tasks_invalid_state_is_400(client):
    resp = client.get("/api/projects/demo/tasks", params={"state": "banana"})
    _assert_error_envelope(resp, 400, "bad_request")


def test_project_tasks_missing_project_is_404(client):
    resp = client.get("/api/projects/nope/tasks")
    _assert_error_envelope(resp, 404, "not_found")


def test_blockers(client, portfolio):
    resp = client.get("/api/blockers")
    assert resp.status_code == 200
    blockers = resp.json()
    assert [b["task"]["id"] for b in blockers] == [portfolio["blocked_id"]]
    assert blockers[0]["project"] == "demo"


def test_active_tasks(client, portfolio):
    resp = client.get("/api/active-tasks")
    assert resp.status_code == 200
    ids = {t["task"]["id"] for t in resp.json()}
    assert portfolio["open_id"] in ids
    assert portfolio["progress_id"] in ids
    assert portfolio["blocked_id"] not in ids


def test_worklog(client):
    resp = client.get("/api/worklog", params={"limit": 5})
    assert resp.status_code == 200
    entries = resp.json()
    assert any(e["summary"] == "did a thing" for e in entries)


# ------------------------------------------------ demoted mutating routes ----

READ_ONLY_ROUTES = [
    ("post", "/api/tasks"),
    ("post", "/api/issues"),
    ("post", "/api/log"),
    ("post", "/api/tasks/demo/DEMO-001/state"),
    ("post", "/api/tasks/demo/DEMO-001/respond"),
    ("post", "/api/projects/demo/pause"),
    ("post", "/api/projects/demo/resume"),
]


@pytest.mark.parametrize("method,path", READ_ONLY_ROUTES)
def test_mutating_routes_are_read_only_405(client, method, path):
    resp = getattr(client, method)(path, json={})
    _assert_error_envelope(resp, 405, "read_only")


def test_create_task_writes_nothing(client, portfolio):
    before = list((portfolio["repo"] / ".project" / "tasks").glob("*.md"))
    resp = client.post(
        "/api/tasks", json={"project": "demo", "title": "Sneaky side-door task"}
    )
    assert resp.status_code == 405
    after = list((portfolio["repo"] / ".project" / "tasks").glob("*.md"))
    assert before == after


def test_create_issue_writes_no_issues_file(client, portfolio):
    resp = client.post(
        "/api/issues", json={"project": "demo", "type": "bug", "severity": "low"}
    )
    assert resp.status_code == 405
    assert not (portfolio["repo"] / ".agent" / "issues.jsonl").exists()


def test_pause_does_not_touch_settings(client, portfolio):
    original = portfolio["settings_path"].read_text(encoding="utf-8")
    resp = client.post("/api/projects/demo/pause")
    assert resp.status_code == 405
    assert portfolio["settings_path"].read_text(encoding="utf-8") == original


# ------------------------------------------------------- error envelope ----

def test_unknown_path_is_envelope_404(client):
    resp = client.get("/api/does-not-exist")
    _assert_error_envelope(resp, 404)


def test_wrong_method_is_envelope_405(client):
    # GET on a POST-only demoted route -> FastAPI 405, reshaped to the envelope.
    resp = client.get("/api/issues")
    _assert_error_envelope(resp, 405)


def test_wrong_method_preserves_allow_header(client):
    # The envelope reshaping must not drop the standard method-discovery header.
    resp = client.get("/api/issues")
    assert resp.status_code == 405
    assert "allow" in {k.lower() for k in resp.headers}


def test_read_route_internal_error_is_500_envelope(portfolio, monkeypatch):
    from clawpm import serve

    def _boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(serve, "discover_projects", _boom)
    client = TestClient(create_app(), raise_server_exceptions=False)
    resp = client.get("/api/projects")
    _assert_error_envelope(resp, 500, "internal_error")


# -------------------------------------------- CLI graceful degradation ----

def test_serve_without_web_extra_exits_gracefully(monkeypatch):
    from clawpm import cli
    from clawpm.cli import serve as serve_mod

    def _boom():
        raise ImportError("No module named 'fastapi'")

    monkeypatch.setattr(serve_mod, "_load_web_server", _boom)
    result = CliRunner().invoke(cli.main, ["serve"])
    assert result.exit_code == 1
    assert "web" in result.output
    assert "pip install" in result.output


def test_load_web_server_returns_app_factory():
    from clawpm.cli import _load_web_server

    create_app_fn, uvicorn_mod = _load_web_server()
    assert callable(create_app_fn)
    assert hasattr(uvicorn_mod, "run")
