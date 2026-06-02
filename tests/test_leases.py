"""Tests for crash-safe dispatch leases (CLAWP-039).

Success criteria under test:
  1. A dispatched subtask whose holder stops heartbeating within the lease TTL
     is detected (by sweep) and transitioned per its fallback policy.
  2. Lease TTL + heartbeat timestamps are file-persisted and survive a fresh
     read (process restart).
  3. The fallback taxonomy (requeue / route-secondary / escalate / fail) is
     selectable per lease and each is exercised.
  4. No daemon — expiry detection rides an explicit sweep (injected `now`).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from clawpm import leases
from clawpm.discovery import load_portfolio_config
from clawpm.leases import (
    FallbackPolicy,
    active_leases,
    apply_fallback,
    expired_leases,
    get_lease,
    grant_lease,
    heartbeat,
    release_lease,
    sweep,
)
from clawpm.models import Predictions, TaskState
from clawpm.tasks import add_task, change_task_state, get_task


@pytest.fixture
def portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_lease_test_")
    root = Path(temp_dir)
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{(root / "projects").as_posix()}"]\n'
        "[defaults]\nstatus = \"active\"\n"
    )
    proj = root / "projects" / "p"
    (proj / ".project").mkdir(parents=True)
    (proj / ".project" / "settings.toml").write_text(
        'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
    )
    tasks_dir = proj / ".project" / "tasks"
    for sub in ("progress", "done", "blocked"):
        (tasks_dir / sub).mkdir(parents=True)

    old = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(root)
    config = load_portfolio_config(root)
    yield {"root": root, "config": config}
    if old:
        os.environ["CLAWPM_PORTFOLIO"] = old
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


def _dispatched_task(config, in_state=TaskState.PROGRESS):
    task = add_task(config, "test", title="leased work",
                    predictions=Predictions(success_criteria=["c"]))
    if in_state != TaskState.OPEN:
        change_task_state(config, "test", task.id, in_state)
    return task.id


# ---------------------------------------------------------------------------
# Persistence + replay (criterion 2)
# ---------------------------------------------------------------------------


class TestPersistenceAndReplay:
    def test_grant_then_fresh_read_recovers_lease(self, portfolio):
        root = portfolio["root"]
        grant_lease(root, "TASK-1", "test", ttl_seconds=300,
                    fallback_policy=FallbackPolicy.REQUEUE, holder_id="wt-1")
        # Fresh replay = "after restart": only the on-disk JSONL is read.
        lease = get_lease(root, "TASK-1", "test")
        assert lease is not None
        assert lease.ttl_seconds == 300
        assert lease.fallback_policy is FallbackPolicy.REQUEUE
        assert lease.holder_id == "wt-1"
        assert lease.active
        # The granted timestamp is also the first heartbeat.
        assert lease.last_heartbeat_at == lease.granted_at

    def test_heartbeat_advances_liveness(self, portfolio):
        root = portfolio["root"]
        grant_lease(root, "T", "test", ttl_seconds=300, fallback_policy=FallbackPolicy.FAIL)
        before = get_lease(root, "T", "test").last_heartbeat_at
        heartbeat(root, "T", "test")
        after = get_lease(root, "T", "test").last_heartbeat_at
        assert after >= before

    def test_grant_requires_positive_ttl(self, portfolio):
        with pytest.raises(ValueError):
            grant_lease(portfolio["root"], "T", "test", ttl_seconds=0,
                        fallback_policy=FallbackPolicy.FAIL)


# ---------------------------------------------------------------------------
# Expiry detection (criterion 4 — injected now, no daemon)
# ---------------------------------------------------------------------------


class TestExpiryDetection:
    def test_within_ttl_not_expired(self, portfolio):
        root = portfolio["root"]
        grant_lease(root, "T", "test", ttl_seconds=600, fallback_policy=FallbackPolicy.FAIL)
        lease = get_lease(root, "T", "test")
        soon = lease.granted_at + timedelta(seconds=60)
        assert lease.is_expired(soon) is False
        assert expired_leases(root, soon) == []

    def test_past_ttl_is_expired(self, portfolio):
        root = portfolio["root"]
        grant_lease(root, "T", "test", ttl_seconds=60, fallback_policy=FallbackPolicy.FAIL)
        lease = get_lease(root, "T", "test")
        later = lease.granted_at + timedelta(seconds=120)
        assert lease.is_expired(later) is True
        assert [l.task_id for l in expired_leases(root, later)] == ["T"]

    def test_is_expired_keys_off_last_heartbeat_not_grant(self):
        # The crux of "heartbeat resets the window": expiry is measured from the
        # last heartbeat, not the grant. Construct directly for determinism.
        from clawpm.leases import Lease, LeaseStatus
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lease = Lease(
            task_id="T", project_id="p", holder_id=None,
            granted_at=t0, ttl_seconds=60,
            fallback_policy=FallbackPolicy.FAIL,
            last_heartbeat_at=t0 + timedelta(seconds=50),  # a beat 50s in
            status=LeaseStatus.ACTIVE,
        )
        # 100s after grant (would be expired vs grant) but only 50s after the
        # heartbeat → NOT expired. Proves the window resets on heartbeat.
        assert lease.is_expired(t0 + timedelta(seconds=100)) is False
        # 70s after the heartbeat → expired.
        assert lease.is_expired(t0 + timedelta(seconds=120)) is True

    def test_expiry_boundary_age_equals_ttl_not_expired(self):
        from clawpm.leases import Lease, LeaseStatus
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lease = Lease(task_id="T", project_id="p", holder_id=None,
                      granted_at=t0, ttl_seconds=60, fallback_policy=FallbackPolicy.FAIL,
                      last_heartbeat_at=t0, status=LeaseStatus.ACTIVE)
        assert lease.is_expired(t0 + timedelta(seconds=60)) is False   # == ttl
        assert lease.is_expired(t0 + timedelta(seconds=61)) is True    # > ttl

    def test_lease_rejects_heartbeat_before_grant(self):
        from clawpm.leases import Lease, LeaseStatus
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            Lease(task_id="T", project_id="p", holder_id=None,
                  granted_at=t0, ttl_seconds=60, fallback_policy=FallbackPolicy.FAIL,
                  last_heartbeat_at=t0 - timedelta(seconds=1), status=LeaseStatus.ACTIVE)


# ---------------------------------------------------------------------------
# Fallback taxonomy (criteria 1 + 3)
# ---------------------------------------------------------------------------


class TestFallbackTaxonomy:
    @pytest.mark.parametrize("policy,expected_state", [
        (FallbackPolicy.REQUEUE, TaskState.OPEN),
        (FallbackPolicy.ROUTE_SECONDARY, TaskState.OPEN),
        (FallbackPolicy.ESCALATE, TaskState.BLOCKED),
        (FallbackPolicy.FAIL, TaskState.BLOCKED),
    ])
    def test_each_policy_transitions_task(self, portfolio, policy, expected_state):
        config, root = portfolio["config"], portfolio["root"]
        task_id = _dispatched_task(config, in_state=TaskState.PROGRESS)
        grant_lease(root, task_id, "test", ttl_seconds=60, fallback_policy=policy)
        lease = get_lease(root, task_id, "test")
        later = lease.granted_at + timedelta(seconds=120)

        actions = sweep(config, root, now=later)

        assert len(actions) == 1
        assert actions[0]["policy"] == policy.value
        moved = get_task(config, "test", task_id)
        assert moved.state == expected_state
        # Lease is now terminal (reassigned) — no longer active, won't re-sweep.
        assert get_lease(root, task_id, "test").active is False
        assert sweep(config, root, now=later + timedelta(seconds=60)) == []

    def test_reassigned_event_records_resolution(self, portfolio):
        # The durable record of WHY/HOW a lease was resolved is the reassigned
        # event in leases.jsonl (change_task_state's note is not persisted to
        # the task body). `clawpm lease list` surfaces it.
        import json
        config, root = portfolio["config"], portfolio["root"]
        task_id = _dispatched_task(config)
        grant_lease(root, task_id, "test", ttl_seconds=30, fallback_policy=FallbackPolicy.ESCALATE)
        lease = get_lease(root, task_id, "test")
        sweep(config, root, now=lease.granted_at + timedelta(seconds=90))
        events = [
            json.loads(l)
            for l in (root / leases.LEASE_REGISTRY_FILENAME).read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        reassigned = [e for e in events if e["action"] == "reassigned" and e["task_id"] == task_id]
        assert len(reassigned) == 1
        assert reassigned[0]["fallback_policy"] == "escalate-to-human"
        assert reassigned[0]["resulting_state"] == "blocked"
        assert "last_heartbeat_at" in reassigned[0]


# ---------------------------------------------------------------------------
# Lifecycle: release + active filtering
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_released_lease_is_not_swept(self, portfolio):
        config, root = portfolio["config"], portfolio["root"]
        task_id = _dispatched_task(config)
        grant_lease(root, task_id, "test", ttl_seconds=10, fallback_policy=FallbackPolicy.FAIL)
        release_lease(root, task_id, "test")
        lease = get_lease(root, task_id, "test")
        assert lease.active is False
        # Even well past TTL, a released lease never triggers a fallback.
        assert sweep(config, root, now=lease.granted_at + timedelta(seconds=999)) == []
        assert get_task(config, "test", task_id).state == TaskState.PROGRESS

    def test_regrant_after_reassign_starts_fresh(self, portfolio):
        config, root = portfolio["config"], portfolio["root"]
        task_id = _dispatched_task(config)
        grant_lease(root, task_id, "test", ttl_seconds=60, fallback_policy=FallbackPolicy.REQUEUE)
        lease = get_lease(root, task_id, "test")
        sweep(config, root, now=lease.granted_at + timedelta(seconds=120))
        assert get_lease(root, task_id, "test").active is False
        # Re-dispatch: a new grant supersedes the terminal lease.
        grant_lease(root, task_id, "test", ttl_seconds=60, fallback_policy=FallbackPolicy.FAIL)
        fresh = get_lease(root, task_id, "test")
        assert fresh.active is True
        assert fresh.fallback_policy is FallbackPolicy.FAIL

    def test_open_leased_task_is_reaped(self, portfolio):
        # Codex P1: `tasks dispatch --lease-ttl` does not move the task to
        # PROGRESS, so a leased task is commonly still OPEN while the holder
        # works. A dead holder on an OPEN task MUST trigger the fallback.
        config, root = portfolio["config"], portfolio["root"]
        task = add_task(config, "test", title="open work",
                        predictions=Predictions(success_criteria=["c"]))
        # Left OPEN (not moved to progress).
        grant_lease(root, task.id, "test", ttl_seconds=30, fallback_policy=FallbackPolicy.ESCALATE)
        lease = get_lease(root, task.id, "test")
        actions = sweep(config, root, now=lease.granted_at + timedelta(seconds=90))
        assert len(actions) == 1
        assert actions[0]["transitioned"] is True
        assert not actions[0].get("retired_without_fallback")
        assert get_task(config, "test", task.id).state == TaskState.BLOCKED

    def test_finished_task_not_yanked_back_by_stale_lease(self, portfolio):
        # Holder completed the work (task -> done) but crashed before releasing
        # the lease. A later sweep must retire the lease, NOT move the done task.
        config, root = portfolio["config"], portfolio["root"]
        task_id = _dispatched_task(config)
        grant_lease(root, task_id, "test", ttl_seconds=30, fallback_policy=FallbackPolicy.REQUEUE)
        change_task_state(config, "test", task_id, TaskState.DONE)
        lease = get_lease(root, task_id, "test")
        actions = sweep(config, root, now=lease.granted_at + timedelta(seconds=90))
        assert len(actions) == 1
        assert actions[0]["retired_without_fallback"] is True
        assert get_task(config, "test", task_id).state == TaskState.DONE
        assert get_lease(root, task_id, "test").active is False

    def test_active_leases_excludes_terminal(self, portfolio):
        root = portfolio["root"]
        grant_lease(root, "A", "test", ttl_seconds=60, fallback_policy=FallbackPolicy.FAIL)
        grant_lease(root, "B", "test", ttl_seconds=60, fallback_policy=FallbackPolicy.FAIL)
        release_lease(root, "B", "test")
        ids = sorted(l.task_id for l in active_leases(root))
        assert ids == ["A"]


# ---------------------------------------------------------------------------
# Policy parsing
# ---------------------------------------------------------------------------


class TestDispatchIntegration:
    """End-to-end through the CLI: dispatch grants a lease; done releases it."""

    def _portfolio_with_repo(self):
        import subprocess
        temp_dir = tempfile.mkdtemp(prefix="clawpm_lease_disp_")
        root = Path(temp_dir)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / "README.md").write_text("hi", encoding="utf-8")
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a",
                        "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a",
                        "-C", str(repo), "commit", "-qm", "init"], check=True)
        (root / "portfolio.toml").write_text(
            f'portfolio_root = "{root.as_posix()}"\n'
            f'project_roots = ["{root.as_posix()}"]\n[defaults]\nstatus = "active"\n'
        )
        meta = repo / ".project"
        meta.mkdir()
        (meta / "settings.toml").write_text(
            f'id = "test"\nname = "T"\nstatus = "active"\npriority = 3\n'
            f'repo_path = "{repo.as_posix()}"\n'
        )
        for sub in ("progress", "done", "blocked"):
            (meta / "tasks" / sub).mkdir(parents=True)
        return root, repo

    def test_dispatch_with_lease_ttl_grants_and_done_releases(self, monkeypatch):
        import json
        from click.testing import CliRunner
        from clawpm.cli import main

        root, repo = self._portfolio_with_repo()
        monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
        config = load_portfolio_config(root)
        task = add_task(config, "test", title="leased",
                        predictions=Predictions(success_criteria=["c"]))
        target = repo / "disp"

        runner = CliRunner()
        r = runner.invoke(main, [
            "tasks", "dispatch", task.id, "--project", "test",
            "--target-dir", str(target), "--no-session-context",
            "--lease-ttl", "120", "--fallback-policy", "escalate-to-human",
        ])
        assert r.exit_code == 0, r.output
        # Lease granted + heartbeat hook wired.
        lease = get_lease(root, task.id, "test")
        assert lease is not None and lease.active
        assert lease.fallback_policy is FallbackPolicy.ESCALATE
        settings = json.loads((target / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        cmds = [h["command"] for h in settings["hooks"]["PostToolUse"][0]["hooks"]]
        assert any("lease heartbeat" in c for c in cmds)

        # The CLI done transition releases the lease, so a completed task is
        # never swept into a fallback.
        rd = runner.invoke(main, ["tasks", "state", task.id, "done", "--project", "test"])
        assert rd.exit_code == 0, rd.output
        assert get_lease(root, task.id, "test").active is False

        shutil.rmtree(root, ignore_errors=True)


class TestDoctorIntegration:
    """CLAWP-039: doctor is the second no-daemon expiry detector."""

    def _expired_lease_setup(self, portfolio):
        config, root = portfolio["config"], portfolio["root"]
        task_id = _dispatched_task(config, in_state=TaskState.PROGRESS)
        grant_lease(root, task_id, "test", ttl_seconds=1, fallback_policy=FallbackPolicy.ESCALATE)
        import time
        time.sleep(1.1)  # let the 1s TTL lapse against real wall-clock
        return config, root, task_id

    def test_doctor_detects_expired_lease(self, portfolio):
        import json
        from click.testing import CliRunner
        from clawpm.cli import main
        _, _, task_id = self._expired_lease_setup(portfolio)
        r = CliRunner().invoke(main, ["--format", "json", "doctor"])
        assert r.exit_code in (0, 1)  # 1 only under --strict, which we didn't pass
        payload = json.loads(r.output)
        assert task_id in [e["task_id"] for e in payload["expired_leases"]]

    def test_doctor_apply_reaps_expired_lease(self, portfolio):
        import json
        from click.testing import CliRunner
        from clawpm.cli import main
        config, root, task_id = self._expired_lease_setup(portfolio)
        r = CliRunner().invoke(main, ["--format", "json", "doctor", "--apply", "--yes"])
        applied = [a for a in json.loads(r.output).get("applied", []) if a["class"] == "lease-expired"]
        assert any(task_id in a["target"] for a in applied)
        assert get_task(config, "test", task_id).state == TaskState.BLOCKED  # escalate
        assert get_lease(root, task_id, "test").active is False


class TestCrossProjectIsolation:
    """Codex CRITICAL: a project-scoped sweep must not reap another project's
    leases. expired_leases/sweep take a project_id filter."""

    def test_expired_leases_scoped_to_project(self, portfolio):
        root = portfolio["root"]
        grant_lease(root, "A", "projA", ttl_seconds=30, fallback_policy=FallbackPolicy.FAIL)
        grant_lease(root, "B", "projB", ttl_seconds=30, fallback_policy=FallbackPolicy.FAIL)
        now = get_lease(root, "A", "projA").granted_at + timedelta(seconds=90)
        scoped = expired_leases(root, now, project_id="projA")
        assert {l.task_id for l in scoped} == {"A"}
        assert {l.task_id for l in expired_leases(root, now)} == {"A", "B"}

    def test_sweep_scoped_does_not_touch_other_project(self, portfolio):
        config, root = portfolio["config"], portfolio["root"]
        # Real task in "test"; a phantom lease in "other" must be left alone.
        task_id = _dispatched_task(config, in_state=TaskState.PROGRESS)
        grant_lease(root, task_id, "test", ttl_seconds=30, fallback_policy=FallbackPolicy.ESCALATE)
        grant_lease(root, "OTHER-1", "other", ttl_seconds=30, fallback_policy=FallbackPolicy.FAIL)
        now = get_lease(root, task_id, "test").granted_at + timedelta(seconds=90)
        actions = sweep(config, root, now=now, project_id="test")
        assert [a["task_id"] for a in actions] == [task_id]
        # The other project's lease is untouched — still active.
        assert get_lease(root, "OTHER-1", "other").active is True


class TestTransitionFailure:
    """Codex HIGH: a transition that raises must NOT retire the lease as a
    success — the task would be orphaned in PROGRESS forever."""

    def test_transition_failure_keeps_lease_active(self, portfolio, monkeypatch):
        config, root = portfolio["config"], portfolio["root"]
        task_id = _dispatched_task(config, in_state=TaskState.PROGRESS)
        grant_lease(root, task_id, "test", ttl_seconds=30, fallback_policy=FallbackPolicy.REQUEUE)
        # Force the move to raise an I/O error (apply_fallback imports
        # change_task_state from clawpm.tasks at call time).
        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr("clawpm.tasks.change_task_state", boom)

        now = get_lease(root, task_id, "test").granted_at + timedelta(seconds=90)
        actions = sweep(config, root, now=now)
        assert len(actions) == 1
        assert actions[0]["transitioned"] is False
        assert "disk full" in actions[0]["transition_error"]
        # Lease NOT retired — still active so the next sweep retries.
        assert get_lease(root, task_id, "test").active is True
        # Task still in PROGRESS (the move failed) — not orphaned silently.
        assert get_task(config, "test", task_id).state == TaskState.PROGRESS

    def test_requeue_tears_down_dispatch_settings(self, portfolio):
        # REQUEUE/ROUTE_SECONDARY with a target_dir tears down stale settings.
        import json
        from clawpm.dispatch import write_dispatch_settings, settings_path
        config, root = portfolio["config"], portfolio["root"]
        task_id = _dispatched_task(config, in_state=TaskState.PROGRESS)
        target = root / "wt"
        write_dispatch_settings(target, task_id, "test", portfolio_root=root)
        assert settings_path(target).exists()
        grant_lease(root, task_id, "test", ttl_seconds=30,
                    fallback_policy=FallbackPolicy.REQUEUE,
                    target_dir=target.as_posix())
        now = get_lease(root, task_id, "test").granted_at + timedelta(seconds=90)
        actions = sweep(config, root, now=now)
        assert actions[0]["transitioned"] is True
        assert not settings_path(target).exists()  # torn down


class TestLeaseCLI:
    """Codex test-gap: the lease command group was entirely untested."""

    def _run(self, args):
        from click.testing import CliRunner
        from clawpm.cli import main
        return CliRunner().invoke(main, ["--format", "json", *args])

    def test_grant_list_release_roundtrip(self, portfolio):
        import json
        task_id = _dispatched_task(portfolio["config"])
        r = self._run(["lease", "grant", "--project", "test", "--task", task_id,
                       "--ttl", "300", "--fallback-policy", "escalate-to-human"])
        assert r.exit_code == 0, r.output
        r2 = self._run(["lease", "list", "--project", "test"])
        leased = json.loads(r2.output)["data"]["leases"]
        assert any(l["task_id"] == task_id and not l["expired"] for l in leased)
        assert self._run(["lease", "release", "--project", "test", "--task", task_id]).exit_code == 0
        leased2 = json.loads(self._run(["lease", "list", "--project", "test"]).output)["data"]["leases"]
        assert task_id not in [l["task_id"] for l in leased2]

    def test_grant_rejects_bad_ttl(self, portfolio):
        task_id = _dispatched_task(portfolio["config"])
        r = self._run(["lease", "grant", "--project", "test", "--task", task_id, "--ttl", "0"])
        assert r.exit_code == 1

    def test_sweep_dry_run_does_not_mutate(self, portfolio):
        config = portfolio["config"]
        task_id = _dispatched_task(config, in_state=TaskState.PROGRESS)
        # ttl=1 + sleep so it's genuinely expired against wall-clock.
        grant_lease(portfolio["root"], task_id, "test", ttl_seconds=1, fallback_policy=FallbackPolicy.ESCALATE)
        import time; time.sleep(1.1)
        r = self._run(["lease", "sweep", "--dry-run"])
        assert r.exit_code == 0
        # dry-run must NOT move the task.
        assert get_task(config, "test", task_id).state == TaskState.PROGRESS
        assert get_lease(portfolio["root"], task_id, "test").active is True


class TestPolicyParsing:
    def test_from_str_roundtrip(self):
        for p in FallbackPolicy:
            assert FallbackPolicy.from_str(p.value) is p

    def test_from_str_rejects_unknown(self):
        with pytest.raises(ValueError, match="unknown fallback policy"):
            FallbackPolicy.from_str("teleport")


# ---------------------------------------------------------------------------
# Corruption resilience (a half-written line must not nuke the sweep)
# ---------------------------------------------------------------------------


class TestCorruptionResilience:
    def test_garbage_line_skipped(self, portfolio):
        root = portfolio["root"]
        grant_lease(root, "T", "test", ttl_seconds=60, fallback_policy=FallbackPolicy.FAIL)
        reg = root / leases.LEASE_REGISTRY_FILENAME
        with open(reg, "a", encoding="utf-8") as f:
            f.write('{"action": "heartbeat", "task_id": "T"\n')  # truncated JSON
        # Replay still recovers the valid grant.
        assert get_lease(root, "T", "test") is not None

    def test_zero_ttl_grant_line_skipped(self, portfolio):
        # Codex P2: a zero/missing ttl is a malformed grant — it must NOT become
        # a real 1s lease the next sweep acts on.
        import json
        root = portfolio["root"]
        reg = root / leases.LEASE_REGISTRY_FILENAME
        reg.write_text(json.dumps({"action": "granted", "task_id": "Z", "project_id": "test",
                                   "ttl_seconds": 0, "fallback_policy": "fail",
                                   "ts": "2026-01-01T00:00:00Z"}) + "\n", encoding="utf-8")
        assert get_lease(root, "Z", "test") is None

    def test_heartbeat_from_replaced_holder_ignored(self, portfolio):
        # Codex P2: a heartbeat from a REPLACED holder must not refresh the
        # active lease (which would mask the new holder's crash). Controlled
        # timestamps make this deterministic.
        import json
        root = portfolio["root"]
        reg = root / leases.LEASE_REGISTRY_FILENAME
        reg.write_text(
            json.dumps({"action": "granted", "task_id": "T", "project_id": "test",
                        "holder_id": "W1", "ttl_seconds": 300, "fallback_policy": "fail",
                        "ts": "2026-01-01T00:00:00Z"}) + "\n"
            + json.dumps({"action": "heartbeat", "task_id": "T", "project_id": "test",
                          "holder_id": "W2", "ts": "2026-01-01T00:01:00Z"}) + "\n",
            encoding="utf-8",
        )
        lease = get_lease(root, "T", "test")
        # The W2 (wrong-holder) beat, though later, did NOT advance the W1 lease.
        assert lease.last_heartbeat_at == leases._parse_ts("2026-01-01T00:00:00Z")
        # A matching W1 beat DOES advance.
        with open(reg, "a", encoding="utf-8") as f:
            f.write(json.dumps({"action": "heartbeat", "task_id": "T", "project_id": "test",
                                "holder_id": "W1", "ts": "2026-01-01T00:02:00Z"}) + "\n")
        assert get_lease(root, "T", "test").last_heartbeat_at == leases._parse_ts("2026-01-01T00:02:00Z")

    def test_holderless_heartbeat_stays_lenient(self, portfolio):
        # A manual/legacy heartbeat with no holder still advances (can't check).
        import json
        root = portfolio["root"]
        reg = root / leases.LEASE_REGISTRY_FILENAME
        reg.write_text(
            json.dumps({"action": "granted", "task_id": "T", "project_id": "test",
                        "holder_id": "W1", "ttl_seconds": 300, "fallback_policy": "fail",
                        "ts": "2026-01-01T00:00:00Z"}) + "\n"
            + json.dumps({"action": "heartbeat", "task_id": "T", "project_id": "test",
                          "ts": "2026-01-01T00:01:00Z"}) + "\n",
            encoding="utf-8",
        )
        assert get_lease(root, "T", "test").last_heartbeat_at == leases._parse_ts("2026-01-01T00:01:00Z")

    def test_bad_ttl_grant_line_skipped_not_aborting(self, portfolio):
        # Codex P2: a corrupted ttl_seconds on ONE grant must skip only that
        # line, never abort the whole replay (which would disable crash
        # detection for every later valid lease).
        import json
        root = portfolio["root"]
        reg = root / leases.LEASE_REGISTRY_FILENAME
        with open(reg, "a", encoding="utf-8") as f:
            f.write(json.dumps({"action": "granted", "task_id": "BAD", "project_id": "test",
                                "ttl_seconds": "not-an-int", "fallback_policy": "fail",
                                "ts": "2026-01-01T00:00:00Z"}) + "\n")
        # A valid grant AFTER the bad line must still be recovered.
        grant_lease(root, "GOOD", "test", ttl_seconds=60, fallback_policy=FallbackPolicy.FAIL)
        assert get_lease(root, "BAD", "test") is None      # malformed → skipped
        assert get_lease(root, "GOOD", "test") is not None  # replay didn't abort
