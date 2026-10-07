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
        # CLAWP-086 — a state rewrite is a mutation, so the synthesized minimal
        # frontmatter also carries an `updated` stamp.
        assert text.startswith("---\nstate: done\n")
        fm = yaml.safe_load(text.split("---", 2)[1])
        assert fm["state"] == "done"
        assert "updated" in fm
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


class TestApplyPhaseOutcomeSeparation:
    """CLAWP-094 — applied[] holds only genuinely applied items."""

    @staticmethod
    def _phase(**kw):
        return TestRunApplyPhase._phase(**kw)

    def test_skipped_result_routed_to_apply_skipped(self):
        applied, skipped = self._phase(
            drift_tasks=[
                {"file": "", "issue": "state_mismatch", "location_state": "open"}
            ]
        )
        assert applied == []
        assert len(skipped) == 1
        assert skipped[0]["class"] == "drift_tasks"
        assert skipped[0]["reason"].startswith("skipped:")
        assert skipped[0]["outcome"] == "skipped"

    def test_error_result_routed_to_apply_skipped_flagged_error(self, tmp_path):
        f = tmp_path / "bad.md"
        f.write_text("---\nstate: blocked\nno close fence\n", encoding="utf-8")
        applied, skipped = self._phase(
            drift_tasks=[
                {"file": str(f), "issue": "state_mismatch", "location_state": "open"}
            ]
        )
        assert applied == []
        assert skipped[0]["outcome"] == "error"
        assert "error" in skipped[0]["reason"]

    def test_noop_cascade_not_reported_as_applied(self, isolated_portfolio):
        tasks = isolated_portfolio.tasks_dir
        _write(
            tasks / "blocked" / "TEST-021.md",
            "blocked",
            task_id="TEST-021",
            extra="depends:\n  - TEST-020\n",
        )
        applied, skipped = self._phase(
            config=isolated_portfolio.config,
            stale_blocked=[
                {"task_id": "TEST-021", "project_id": "test", "deps": ["TEST-020"]}
            ],
        )
        assert applied == []
        assert skipped[0]["class"] == "stale_blocked"
        assert skipped[0]["outcome"] == "skipped"

    def test_real_apply_stays_in_applied(self, tmp_path):
        f = tmp_path / "TEST-001.md"
        _write(f, "open")
        applied, skipped = self._phase(
            drift_tasks=[{"file": str(f), "issue": "half_rename"}]
        )
        assert len(applied) == 1 and skipped == []

    def test_dry_run_would_entries_stay_in_applied(self, tmp_path):
        f = tmp_path / "TEST-001.md"
        _write(f, "open")
        applied, _ = self._phase(
            drift_tasks=[{"file": str(f), "issue": "half_rename"}], dry_run=True
        )
        assert applied[0]["result"].startswith("would-")


class TestStaleBlockedFrontmatterConsistency:
    """CLAWP-094 — promoting via cascade must not leave `state: blocked` behind."""

    def test_cascade_rewrites_state_line(self, isolated_portfolio):
        tasks = isolated_portfolio.tasks_dir
        _write(tasks / "done" / "TEST-030.md", "done", task_id="TEST-030")
        _write(
            tasks / "blocked" / "TEST-031.md",
            "blocked",
            task_id="TEST-031",
            extra="depends:\n  - TEST-030\n# keep me\n",
        )
        res = apply_stale_blocked(
            {"task_id": "TEST-031", "project_id": "test", "deps": ["TEST-030"]},
            config=isolated_portfolio.config,
        )
        assert "promoted" in res["result"]
        text = (tasks / "TEST-031.md").read_text(encoding="utf-8")
        fm = yaml.safe_load(text.split("---", 2)[1])
        assert fm["state"] == "open"
        assert "# keep me" in text  # surgical, not a reserialise

    def test_sibling_sync_failure_is_surfaced(self, isolated_portfolio, monkeypatch, caplog):
        """A sync failure on ANOTHER promoted dependent must not vanish (Codex r2)."""
        import clawpm.doctor_apply as da

        monkeypatch.setattr(
            da,
            "cascade_unblock_dependents",
            lambda *a, **k: [
                {"task_id": "TEST-051"},
                {"task_id": "TEST-052", "state_sync_error": "disk says no"},
            ],
        )
        with caplog.at_level("WARNING"):
            res = apply_stale_blocked(
                {"task_id": "TEST-051", "project_id": "test", "deps": ["TEST-050"]},
                config=isolated_portfolio.config,
            )
        assert "promoted" in res["result"]
        assert "TEST-052" in res["result"] and "disk says no" in res["result"]
        assert any("state sync failed" in m for m in caplog.messages)

    def test_sibling_sync_failure_surfaced_when_target_not_promoted(
        self, isolated_portfolio, monkeypatch
    ):
        import clawpm.doctor_apply as da

        monkeypatch.setattr(
            da,
            "cascade_unblock_dependents",
            lambda *a, **k: [{"task_id": "TEST-062", "state_sync_error": "boom"}],
        )
        res = apply_stale_blocked(
            {"task_id": "TEST-061", "project_id": "test", "deps": ["TEST-060"]},
            config=isolated_portfolio.config,
        )
        assert "did not promote" in res["result"] and "boom" in res["result"]

    def test_cascade_without_state_key_adds_none(self, isolated_portfolio):
        tasks = isolated_portfolio.tasks_dir
        _write(tasks / "done" / "TEST-040.md", "done", task_id="TEST-040")
        (tasks / "blocked" / "TEST-041.md").write_text(
            "---\nid: TEST-041\ntitle: T\ndepends:\n  - TEST-040\n---\n\nbody\n",
            encoding="utf-8",
        )
        apply_stale_blocked(
            {"task_id": "TEST-041", "project_id": "test", "deps": ["TEST-040"]},
            config=isolated_portfolio.config,
        )
        fm = yaml.safe_load(
            (tasks / "TEST-041.md").read_text(encoding="utf-8").split("---", 2)[1]
        )
        assert "state" not in fm

    def test_sync_state_failure_is_marked_not_silent(self, isolated_portfolio, monkeypatch):
        from clawpm import tasks as tasks_mod

        tasks = isolated_portfolio.tasks_dir
        _write(tasks / "done" / "TEST-050.md", "done", task_id="TEST-050")
        _write(
            tasks / "blocked" / "TEST-051.md",
            "blocked",
            task_id="TEST-051",
            extra="depends:\n  - TEST-050\n",
        )

        def boom(*a, **k):
            raise OSError("disk says no")

        monkeypatch.setattr(tasks_mod, "_sync_state_line", boom)
        transitions = tasks_mod.cascade_unblock_dependents(
            isolated_portfolio.config, "test", "TEST-050"
        )
        assert [t["task_id"] for t in transitions] == ["TEST-051"]
        assert "disk says no" in transitions[0]["state_sync_error"]


class TestCascadeSyncRound1Fixes:
    """CLAWP-094 Codex r1: lock scope, multiline YAML values, surfaced failures."""

    @staticmethod
    def _seed(tasks, dep_id, blk_id, state_block):
        _write(tasks / "done" / f"{dep_id}.md", "done", task_id=dep_id)
        (tasks / "blocked" / f"{blk_id}.md").write_bytes(
            (
                f"---\nid: {blk_id}\ntitle: T\n{state_block}"
                f"depends:\n  - {dep_id}\n---\n\nbody\n"
            ).encode("utf-8")
        )

    def test_sync_runs_under_tasks_lock(self, isolated_portfolio, monkeypatch):
        import os

        from clawpm import tasks as tasks_mod
        from clawpm.concurrency import _held_depths

        tasks = isolated_portfolio.tasks_dir
        self._seed(tasks, "TEST-060", "TEST-061", "state: blocked\n")
        key = os.path.normcase(os.path.abspath(str(tasks / ".clawpm-tasks.lock")))
        held = []
        real = tasks_mod._sync_state_line

        def spy(path, new_state):
            held.append(_held_depths().get(key, 0))
            return real(path, new_state)

        monkeypatch.setattr(tasks_mod, "_sync_state_line", spy)
        tasks_mod.cascade_unblock_dependents(
            isolated_portfolio.config, "test", "TEST-060"
        )
        assert held and all(d > 0 for d in held)

    @pytest.mark.parametrize(
        "state_block",
        [
            "state: >-\n  blocked\n",
            "state: |\n  blocked\n",
            'state: "blocked"\n',
            "state: 'blocked'  # note\n",
            "state: >-\r\n  blocked\r\n",
        ],
    )
    def test_multiline_and_quoted_state_fully_rewritten(
        self, isolated_portfolio, state_block
    ):
        from clawpm import tasks as tasks_mod

        tasks = isolated_portfolio.tasks_dir
        if "\r\n" in state_block:
            # whole file CRLF
            (tasks / "done").mkdir(exist_ok=True)
            _write(tasks / "done" / "TEST-070.md", "done", task_id="TEST-070")
            (tasks / "blocked" / "TEST-071.md").write_bytes(
                (
                    "---\r\nid: TEST-071\r\ntitle: T\r\n" + state_block
                    + "depends:\r\n  - TEST-070\r\n---\r\n\r\nbody\r\n"
                ).encode("utf-8")
            )
        else:
            self._seed(tasks, "TEST-070", "TEST-071", state_block)
        res = tasks_mod.cascade_unblock_dependents(
            isolated_portfolio.config, "test", "TEST-070"
        )
        assert "state_sync_error" not in res[0]
        raw = (tasks / "TEST-071.md").read_bytes().decode("utf-8")
        fm = yaml.safe_load(raw.split("---", 2)[1])
        assert fm["state"] == "open"
        assert fm["depends"] == ["TEST-070"]
        assert "blocked" not in raw.split("depends")[0].replace("id:", "")
        if "\r\n" in state_block:
            assert "\n" not in raw.replace("\r\n", "")

    def test_unsupported_state_form_is_refused_with_error(self, isolated_portfolio):
        from clawpm import tasks as tasks_mod

        tasks = isolated_portfolio.tasks_dir
        self._seed(tasks, "TEST-080", "TEST-081", "state:\n  - blocked\n")
        res = tasks_mod.cascade_unblock_dependents(
            isolated_portfolio.config, "test", "TEST-080"
        )
        assert res[0].get("state_sync_error")
        raw = (tasks / "TEST-081.md").read_text(encoding="utf-8")
        assert "  - blocked" in raw  # untouched, not half-rewritten

    def test_sync_error_surfaces_in_cascade_errors(self, isolated_portfolio, monkeypatch):
        from clawpm import tasks as tasks_mod
        from clawpm.services.tasks import transition

        tasks = isolated_portfolio.tasks_dir
        _write(tasks / "TEST-090.md", "open", task_id="TEST-090")
        (tasks / "blocked" / "TEST-091.md").write_text(
            "---\nid: TEST-091\ntitle: T\nstate: blocked\ndepends:\n  - TEST-090\n---\n\nb\n",
            encoding="utf-8",
        )

        def boom(*a, **k):
            raise OSError("disk says no")

        monkeypatch.setattr(tasks_mod, "_sync_state_line", boom)
        res = transition(
            isolated_portfolio.config,
            project_id="test",
            task_id="TEST-090",
            new_state="done",
        )
        assert res["ok"]
        errs = res["data"].get("cascade_errors")
        assert errs and "disk says no" in errs[0]["message"]
        assert errs[0].get("task_id") == "TEST-091"
