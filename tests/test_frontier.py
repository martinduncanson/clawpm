"""CLAWP-111-004: ``next --frontier <root>`` / ``tasks list --frontier <root>``.

Success criteria under test:
  1. JSON carries frontier / blocked [{id, waiting_on}] / claimed [{id,
     lease_holder}], ordered by priority then id; an expired lease does not claim.
  2. Text output prints every task as its title with the id in brackets.
  3. A root with no open children reports the map is clear, exit 0.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from clawpm.cli import main
from clawpm.leases import FallbackPolicy, grant_lease
from clawpm.models import Predictions, TaskState
from clawpm.tasks import add_subtask, add_task, change_task_state, frontier


def _sub(iso, parent, title, **kw):
    return add_subtask(
        iso.config, iso.project_id, parent, title,
        predictions=Predictions(success_criteria=["c"]), **kw,
    )


def _tree(iso):
    """root -> ready(p2), ready_hi(p1), blocked(depends on dep), claimed, nested map -> deep leaf."""
    root = add_task(iso.config, iso.project_id, title="Root map",
                    predictions=Predictions(success_criteria=["c"]))
    dep = _sub(iso, root.id, "Dependency", priority=9)
    ready = _sub(iso, root.id, "Ready later", priority=2)
    ready_hi = _sub(iso, root.id, "Ready first", priority=1)
    blocked = _sub(iso, root.id, "Blocked one", priority=3, depends=[dep.id])
    claimed = _sub(iso, root.id, "Claimed one", priority=4)
    nest = _sub(iso, root.id, "Nested map", priority=5)
    deep = _sub(iso, nest.id, "Deep leaf", priority=6)
    grant_lease(iso.root, claimed.id, iso.project_id, ttl_seconds=3600,
                fallback_policy=FallbackPolicy.REQUEUE, holder_id="wt-7")
    return root, dep, ready, ready_hi, blocked, claimed, nest, deep


def test_frontier_membership_and_order(isolated_portfolio):
    iso = isolated_portfolio
    root, dep, ready, ready_hi, blocked, claimed, nest, deep = _tree(iso)
    res = frontier(iso.config, iso.project_id, root.id)
    assert [t["id"] for t in res["frontier"]] == [ready_hi.id, ready.id, deep.id, dep.id]
    assert res["blocked"] == [{"id": blocked.id, "title": "Blocked one", "waiting_on": [dep.id]}]
    assert res["claimed"] == [{"id": claimed.id, "title": "Claimed one", "lease_holder": "wt-7"}]
    assert nest.id not in {t["id"] for t in res["frontier"]}  # map node, not work


def test_done_dependency_unblocks(isolated_portfolio):
    iso = isolated_portfolio
    root, dep, *_rest = _tree(iso)
    blocked_id = _rest[2].id
    change_task_state(iso.config, iso.project_id, dep.id, TaskState.DONE)
    res = frontier(iso.config, iso.project_id, root.id)
    assert blocked_id in [t["id"] for t in res["frontier"]]
    assert res["blocked"] == []


def test_expired_lease_does_not_claim(isolated_portfolio):
    iso = isolated_portfolio
    root = add_task(iso.config, iso.project_id, title="Root",
                    predictions=Predictions(success_criteria=["c"]))
    leaf = _sub(iso, root.id, "Stale claim")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    event = {"action": "granted", "task_id": leaf.id, "project_id": iso.project_id,
             "holder_id": "dead", "ttl_seconds": 60, "fallback_policy": "requeue",
             "target_dir": None, "ts": old}
    (iso.root / "leases.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    res = frontier(iso.config, iso.project_id, root.id)
    assert [t["id"] for t in res["frontier"]] == [leaf.id]
    assert res["claimed"] == []


def test_cli_json_both_commands(isolated_portfolio):
    iso = isolated_portfolio
    root, dep, ready, ready_hi, blocked, claimed, *_ = _tree(iso)
    runner = CliRunner()
    for argv in (["next", "--frontier", root.id], ["tasks", "list", "--frontier", root.id]):
        res = runner.invoke(main, ["--format", "json", "--project", iso.project_id, *argv])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["frontier"][0]["id"] == ready_hi.id
        assert data["blocked"][0]["waiting_on"] == [dep.id]
        assert data["claimed"][0]["lease_holder"] == "wt-7"


def test_cli_text_never_prints_bare_id(isolated_portfolio):
    iso = isolated_portfolio
    root, dep, ready, ready_hi, blocked, claimed, *_ = _tree(iso)
    res = CliRunner().invoke(
        main, ["--format", "text", "--project", iso.project_id, "next", "--frontier", root.id]
    )
    assert res.exit_code == 0, res.output
    assert f"Ready first [{ready_hi.id}]" in res.output
    assert f"Blocked one [{blocked.id}] waiting on Dependency [{dep.id}]" in res.output
    assert f"Claimed one [{claimed.id}] held by wt-7" in res.output
    for line in res.output.splitlines():
        if line.startswith("  "):
            assert not line.strip().startswith(root.id.split("-")[0] + "-")


def test_empty_map_is_clear_exit_zero(isolated_portfolio):
    iso = isolated_portfolio
    root = add_task(iso.config, iso.project_id, title="Lonely root",
                    predictions=Predictions(success_criteria=["c"]))
    for fmt in ("json", "text"):
        res = CliRunner().invoke(
            main, ["--format", fmt, "--project", iso.project_id, "tasks", "list", "--frontier", root.id]
        )
        assert res.exit_code == 0, res.output
        assert "clear" in res.output.lower()


def test_unknown_root_errors(isolated_portfolio):
    iso = isolated_portfolio
    res = CliRunner().invoke(
        main, ["--project", iso.project_id, "next", "--frontier", "NOPE-999"]
    )
    assert res.exit_code == 1
