"""Auto-ID allocation regression tests (CLAWP-047).

The headline bug: a project id whose ``upper()[:5]`` prefix contains a hyphen
(``arb-prd`` -> ``ARB-P``) broke the ``.md`` number parser — it did
``f.stem.split("-")[1]`` which grabbed ``"P"`` from ``ARB-P-000``, raised
ValueError, skipped EVERY file, and collapsed every new task to ``ARB-P-000``,
silently overwriting prior tasks. The directory scan beside it used an anchored
regex and was correct; the fix unifies the two.

Non-hyphenated prefixes (``clawpm`` -> ``CLAWP``) were never affected — which is
why the project dogfooding clawpm never saw it but ``arb-prd`` did.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main


def _make_portfolio(tmp_path: Path, monkeypatch, project_id: str) -> Path:
    """Register a single project (dir name == id, so it's the canonical dir) and
    point CLAWPM_PORTFOLIO at it. Returns the project's tasks dir."""
    (tmp_path / "portfolio.toml").write_text(
        f'portfolio_root = "{tmp_path.as_posix()}"\n'
        f'project_roots = ["{(tmp_path / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )
    proj_meta = tmp_path / "projects" / project_id / ".project"
    tasks_dir = proj_meta / "tasks"
    (tasks_dir / "done").mkdir(parents=True)
    (tasks_dir / "blocked").mkdir(parents=True)
    (proj_meta / "settings.toml").write_text(
        f'id = "{project_id}"\nname = "{project_id}"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))
    return tasks_dir


def _add(project_id: str, title: str) -> str:
    """Run `clawpm tasks add` and return the allocated id."""
    res = CliRunner().invoke(
        main, ["--format", "json", "tasks", "add", "--project", project_id, "--title", title]
    )
    assert res.exit_code == 0, res.output
    return json.loads(res.output)["data"]["id"]


class TestHyphenatedPrefixCollision:
    def test_sequential_ids_not_collision(self, tmp_path, monkeypatch):
        # prefix = "arb-prd".upper()[:5] = "ARB-P" (hyphen at index 3).
        _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        ids = [_add("arb-prd", f"epic {i}") for i in range(3)]
        assert ids == ["ARB-P-000", "ARB-P-001", "ARB-P-002"], ids
        # The headline invariant: no two epics share an id.
        assert len(set(ids)) == 3

    def test_counts_done_and_progress_files(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        # A completed task in done/ and an in-progress task (.progress.md stem
        # is "ARB-P-003.progress") must both be counted for hyphenated prefixes.
        (tasks_dir / "done" / "ARB-P-005.md").write_text("---\nid: ARB-P-005\n---\n", encoding="utf-8")
        (tasks_dir / "ARB-P-003.progress.md").write_text("---\nid: ARB-P-003\n---\n", encoding="utf-8")
        assert _add("arb-prd", "next") == "ARB-P-006"

    def test_subtask_files_do_not_pollute_top_level(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        # A stray subtask-shaped file at the top level must NOT be read as
        # top-level number 1 (anchored pattern rejects the extra segment).
        (tasks_dir / "ARB-P-000-001.md").write_text("---\nid: ARB-P-000-001\n---\n", encoding="utf-8")
        assert _add("arb-prd", "first real") == "ARB-P-000"


class TestNonHyphenatedPrefixUnaffected:
    def test_plain_prefix_still_sequential(self, tmp_path, monkeypatch):
        # The common case (no hyphen in the first 5 chars) must keep working.
        _make_portfolio(tmp_path, monkeypatch, "test")
        ids = [_add("test", f"t{i}") for i in range(2)]
        assert ids == ["TEST-000", "TEST-001"], ids


# ---------------------------------------------------------------------------
# CLAWP-048: cross-project prefix uniqueness (near-name-twin projects must not
# share an ID namespace) + explicit task_prefix override + doctor detection.
# ---------------------------------------------------------------------------


def _add_project(tmp_path, project_id, task_prefix=None):
    """Add a second project to an existing portfolio."""
    meta = tmp_path / "projects" / project_id / ".project"
    (meta / "tasks" / "done").mkdir(parents=True)
    (meta / "tasks" / "blocked").mkdir(parents=True)
    body = f'id = "{project_id}"\nname = "{project_id}"\nstatus = "active"\npriority = 3\n'
    if task_prefix:
        body += f'task_prefix = "{task_prefix}"\n'
    (meta / "settings.toml").write_text(body, encoding="utf-8")


def _set_task_prefix(tmp_path, project_id, prefix):
    meta = tmp_path / "projects" / project_id / ".project" / "settings.toml"
    meta.write_text(
        f'id = "{project_id}"\nname = "{project_id}"\nstatus = "active"\n'
        f'priority = 3\ntask_prefix = "{prefix}"\n',
        encoding="utf-8",
    )


class TestPrefixUniqueness:
    def test_near_twin_projects_get_distinct_namespaces(self, tmp_path, monkeypatch):
        # arb-prd and arb-prod both derive [:5] = "ARB-P". The second must
        # extend to a collision-free prefix rather than share the namespace.
        _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        first = _add("arb-prd", "epic")           # ARB-P-000, pins arb-prd -> ARB-P
        _add_project(tmp_path, "arb-prod")
        twin = _add("arb-prod", "epic")
        assert first == "ARB-P-000"
        assert not twin.startswith("ARB-P-"), twin   # distinct namespace
        assert twin.startswith("ARB-PR"), twin       # shortest collision-free extension

    def test_explicit_task_prefix_overrides_derivation(self, tmp_path, monkeypatch):
        _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        _set_task_prefix(tmp_path, "arb-prd", "ARBPRD")
        assert _add("arb-prd", "x") == "ARBPRD-000"
        assert _add("arb-prd", "y") == "ARBPRD-001"

    def test_third_twin_avoids_an_extended_prefix_not_just_base(self, tmp_path, monkeypatch):
        # Discriminating case (pins the resolve_existing_prefix .project path):
        # arb-prd -> ARB-P, arb-prod -> extended ARB-PR. A THIRD twin must see
        # arb-prod's REAL minted prefix (ARB-PR), not its [:5], and avoid BOTH.
        _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        p1 = _add("arb-prd", "e")            # ARB-P
        _add_project(tmp_path, "arb-prod")
        p2 = _add("arb-prod", "e")           # extended (ARB-PR...) != ARB-P
        _add_project(tmp_path, "arb-production")
        p3 = _add("arb-production", "e")
        pre = lambda tid: tid.rsplit("-", 1)[0]
        prefixes = {pre(p1), pre(p2), pre(p3)}
        assert len(prefixes) == 3, (p1, p2, p3)  # all three namespaces distinct
        # p3 must NOT reuse arb-prod's extended prefix (the bug the .project
        # path fix closes — with the bug, p3 collided with ARB-PR).
        assert pre(p3) != pre(p2), (p2, p3)

    def test_existing_project_prefix_is_stable_when_twin_appears(self, tmp_path, monkeypatch):
        # arb-prd minted ARB-P; a twin appears later. arb-prd must KEEP ARB-P
        # (inference), never silently re-derive into a longer prefix.
        _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        assert _add("arb-prd", "a") == "ARB-P-000"
        _add_project(tmp_path, "arb-prod")
        _add("arb-prod", "b")  # twin takes an extended prefix
        assert _add("arb-prd", "c") == "ARB-P-001"  # arb-prd unchanged


class TestDoctorCollisionCheck:
    def _prefix_collisions(self, res_output):
        # doctor JSON may be the last JSON object on stdout.
        for chunk in res_output.strip().split("\n\n"):
            try:
                data = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if "prefix_collisions" in data:
                return data["prefix_collisions"]
        return None

    def test_doctor_flags_preexisting_collision(self, tmp_path, monkeypatch):
        # Simulate a pre-CLAWP-048 collision: two projects each already minted
        # ARB-P directly. doctor (resolved-prefix check) must flag it.
        _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        _add_project(tmp_path, "arb-prod")
        for pid in ("arb-prd", "arb-prod"):
            (tmp_path / "projects" / pid / ".project" / "tasks" / "ARB-P-000.md").write_text(
                "---\nid: ARB-P-000\n---\n", encoding="utf-8"
            )
        res = CliRunner().invoke(main, ["--format", "json", "doctor"])
        cols = self._prefix_collisions(res.output)
        assert cols is not None, res.output
        arbp = [c for c in cols if c["prefix"] == "ARB-P"]
        assert arbp and set(arbp[0]["projects"]) == {"arb-prd", "arb-prod"}, cols

    def test_task_prefix_clears_false_collision(self, tmp_path, monkeypatch):
        # Same near-twins, but arb-prod sets task_prefix -> NO collision.
        _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        _add_project(tmp_path, "arb-prod", task_prefix="ARBPROD")
        res = CliRunner().invoke(main, ["--format", "json", "doctor"])
        cols = self._prefix_collisions(res.output)
        assert cols is not None, res.output
        assert not any(c["prefix"] == "ARB-P" and len(c["projects"]) > 1 for c in cols), cols
