"""Unit-level tests for the doctor auto-remediation arms (CLAWP-081).

The existing test_doctor_apply.py drives everything through the ``clawpm doctor
--apply`` CLI. These target the mutating remediation functions in
``clawpm.doctor_apply`` directly, so a regression in an arm surfaces without the
full CLI/doctor-scan round trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from clawpm.doctor_apply import (
    SKIP_REASONS,
    _rewrite_frontmatter_state,
    apply_drift,
    apply_stale_blocked,
    run_apply_phase,
)


def _write(path: Path, state: str, task_id: str = "TEST-001", extra: str = "") -> None:
    path.write_text(
        "---\n"
        f"id: {task_id}\n"
        "title: T\n"
        f"state: {state}\n"
        "priority: 5\n"
        f"{extra}"
        "---\n\n# body\n",
        encoding="utf-8",
    )


class TestRewriteFrontmatterState:
    def test_rewrites_state_preserving_other_fields(self, tmp_path):
        f = tmp_path / "t.md"
        _write(f, "blocked")
        _rewrite_frontmatter_state(f, "open")
        fm = yaml.safe_load(f.read_text(encoding="utf-8").split("---", 2)[1])
        assert fm["state"] == "open"
        assert fm["id"] == "TEST-001"
        assert fm["priority"] == 5

    def test_no_frontmatter_synthesizes_minimal(self, tmp_path):
        f = tmp_path / "t.md"
        f.write_text("just a body, no frontmatter\n", encoding="utf-8")
        _rewrite_frontmatter_state(f, "done")
        text = f.read_text(encoding="utf-8")
        assert text.startswith("---\nstate: done\n---")
        assert "just a body" in text

    def test_malformed_frontmatter_raises(self, tmp_path):
        f = tmp_path / "t.md"
        f.write_text("---\nstate: open\n", encoding="utf-8")  # no closing ---
        with pytest.raises(ValueError):
            _rewrite_frontmatter_state(f, "done")

    def test_no_tmp_left_behind_on_success(self, tmp_path):
        f = tmp_path / "t.md"
        _write(f, "blocked")
        _rewrite_frontmatter_state(f, "open")
        assert not (tmp_path / "t.md.tmp").exists()


class TestApplyDriftHalfRename:
    def test_deletes_bare_md(self, tmp_path):
        f = tmp_path / "TEST-001.md"
        _write(f, "open")
        res = apply_drift({"file": str(f), "issue": "half_rename"})
        assert "deleted" in res["result"].lower()
        assert not f.exists()

    def test_dry_run_keeps_file(self, tmp_path):
        f = tmp_path / "TEST-001.md"
        _write(f, "open")
        res = apply_drift({"file": str(f), "issue": "half_rename"}, dry_run=True)
        assert res["result"].startswith("would-")
        assert f.exists()

    def test_missing_file_skipped(self, tmp_path):
        f = tmp_path / "gone.md"
        res = apply_drift({"file": str(f), "issue": "half_rename"})
        assert "no longer exists" in res["result"]


class TestApplyDriftStateMismatch:
    def test_rewrites_to_location_state(self, tmp_path):
        f = tmp_path / "TEST-002.md"
        _write(f, "blocked", task_id="TEST-002")
        res = apply_drift(
            {
                "file": str(f),
                "issue": "state_mismatch",
                "location_state": "open",
                "frontmatter_state": "blocked",
            }
        )
        assert "rewrote" in res["result"].lower()
        fm = yaml.safe_load(f.read_text(encoding="utf-8").split("---", 2)[1])
        assert fm["state"] == "open"

    def test_dry_run_does_not_mutate(self, tmp_path):
        f = tmp_path / "TEST-002.md"
        _write(f, "blocked", task_id="TEST-002")
        original = f.read_text(encoding="utf-8")
        res = apply_drift(
            {
                "file": str(f),
                "issue": "state_mismatch",
                "location_state": "open",
            },
            dry_run=True,
        )
        assert res["result"].startswith("would-")
        assert f.read_text(encoding="utf-8") == original

    def test_missing_location_state_skipped(self, tmp_path):
        f = tmp_path / "TEST-002.md"
        _write(f, "blocked", task_id="TEST-002")
        res = apply_drift({"file": str(f), "issue": "state_mismatch"})
        assert "location_state missing" in res["result"]


class TestApplyDriftEdgeCases:
    def test_no_file_path(self):
        res = apply_drift({"issue": "half_rename"})
        assert res["result"].startswith("skipped: no file path")

    def test_unknown_issue(self, tmp_path):
        f = tmp_path / "x.md"
        res = apply_drift({"file": str(f), "issue": "weird"})
        assert "unknown drift issue" in res["result"]


class TestApplyStaleBlocked:
    def test_missing_ids_skipped(self):
        res = apply_stale_blocked({"deps": ["X"]}, config=None)
        assert "missing task_id or project_id" in res["result"]

    def test_no_deps_skipped(self):
        res = apply_stale_blocked(
            {"task_id": "T-1", "project_id": "test"}, config=None
        )
        assert "no deps recorded" in res["result"]

    def test_dry_run_short_circuits(self):
        res = apply_stale_blocked(
            {"task_id": "T-1", "project_id": "test", "deps": ["T-0"]},
            config=None,
            dry_run=True,
        )
        assert res["result"].startswith("would-cascade")

    def test_real_cascade_promotes(self, isolated_portfolio):
        tasks = isolated_portfolio.tasks_dir
        _write(tasks / "done" / "TEST-010.md", "done", task_id="TEST-010")
        _write(
            tasks / "blocked" / "TEST-011.md",
            "blocked",
            task_id="TEST-011",
            extra="depends:\n  - TEST-010\n",
        )
        res = apply_stale_blocked(
            {"task_id": "TEST-011", "project_id": "test", "deps": ["TEST-010"]},
            config=isolated_portfolio.config,
        )
        assert "promoted" in res["result"].lower()
        assert not (tasks / "blocked" / "TEST-011.md").exists()
        assert (tasks / "TEST-011.md").exists()


class TestRunApplyPhase:
    @staticmethod
    def _phase(**overrides):
        base = dict(
            config=None,
            drift_tasks=[],
            stale_blocked=[],
            stale_tasks=[],
            prefix_collisions=[],
            unreadable_files=[],
            commit_drift=[],
            missing_markers=[],
            codex_availability=[],
        )
        base.update(overrides)
        return run_apply_phase(**base)

    def test_half_rename_routes_to_applied(self, tmp_path):
        f = tmp_path / "TEST-001.md"
        _write(f, "open")
        applied, skipped = self._phase(
            drift_tasks=[{"file": str(f), "issue": "half_rename"}]
        )
        assert len(applied) == 1
        assert applied[0]["class"] == "drift_tasks"
        assert not f.exists()

    def test_half_rename_disabled_flag_skips(self, tmp_path):
        f = tmp_path / "TEST-001.md"
        _write(f, "open")
        applied, skipped = self._phase(
            drift_tasks=[{"file": str(f), "issue": "half_rename"}],
            apply_half_rename_flag=False,
        )
        assert applied == []
        assert skipped[0]["reason"] == "disabled by --no-apply-half-rename"
        assert f.exists()

    def test_state_mismatch_disabled_flag_skips(self, tmp_path):
        f = tmp_path / "TEST-002.md"
        _write(f, "blocked", task_id="TEST-002")
        applied, skipped = self._phase(
            drift_tasks=[
                {"file": str(f), "issue": "state_mismatch", "location_state": "open"}
            ],
            apply_drift_flag=False,
        )
        assert applied == []
        assert "no-apply-drift" in skipped[0]["reason"]

    def test_unknown_drift_issue_skipped(self):
        applied, skipped = self._phase(
            drift_tasks=[{"file": "x.md", "issue": "bogus"}]
        )
        assert applied == []
        assert "unknown drift issue" in skipped[0]["reason"]

    def test_stale_blocked_disabled_flag_skips(self):
        applied, skipped = self._phase(
            stale_blocked=[{"task_id": "T-1"}],
            apply_cascade_flag=False,
        )
        assert applied == []
        assert "no-apply-cascade" in skipped[0]["reason"]

    def test_non_applyable_classes_use_skip_reasons(self):
        applied, skipped = self._phase(
            stale_tasks=[{"task_id": "S-1"}],
            prefix_collisions=[{"prefix": "AB"}],
            unreadable_files=[{"file": "bad.md"}],
            commit_drift=[{"project_id": "p"}],
            missing_markers=[{"project_id": "p"}],
            codex_availability=[{"project_id": "p"}],
        )
        assert applied == []
        by_class = {s["class"]: s for s in skipped}
        assert by_class["stale_tasks"]["reason"] == SKIP_REASONS["stale_tasks"]
        assert by_class["prefix_collisions"]["reason"] == SKIP_REASONS["prefix_collisions"]
        assert by_class["unreadable_files"]["reason"] == SKIP_REASONS["unreadable_files"]
        assert by_class["commit_drift"]["reason"] == SKIP_REASONS["commit_drift"]
        assert by_class["missing_markers"]["reason"] == SKIP_REASONS["missing_markers"]
        assert by_class["codex_availability"]["reason"] == SKIP_REASONS["codex_availability"]
