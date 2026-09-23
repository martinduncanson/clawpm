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


class TestHyphenOnSliceBoundary:
    """CLAWP-096: a project id whose ``upper()[:5]`` slice lands EXACTLY on the
    hyphen (e.g. "code-quorum", where "code" is 4 chars) must not carry a
    trailing hyphen into the prefix — that doubles the separator once
    ``-{num:03d}`` is appended ("CODE-" + "-000" -> "CODE--000")."""

    def test_slice_boundary_hyphen_is_stripped(self, tmp_path, monkeypatch):
        _make_portfolio(tmp_path, monkeypatch, "code-quorum")
        ids = [_add("code-quorum", f"t{i}") for i in range(2)]
        assert ids == ["CODE-000", "CODE-001"], ids
        assert "--" not in ids[0]

    def test_internal_hyphen_still_preserved(self, tmp_path, monkeypatch):
        # Regression guard: the fix must not regress CLAWP-047's intentional
        # internal-hyphen behaviour ("arb-prd" -> "ARB-P", hyphen mid-prefix).
        _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        assert _add("arb-prd", "epic") == "ARB-P-000"

    def test_two_taskless_siblings_on_the_same_slice_boundary_do_not_collide(
        self, tmp_path, monkeypatch
    ):
        # Code-quorum review finding: _portfolio_prefixes' collision-set
        # placeholder for a still task-less sibling must agree with what
        # assign_task_prefix's OWN candidate strips to, or two siblings that
        # both slice-with-boundary-hyphen to "CODE" (here: "code-quorum" and
        # "code-runner", both registered but neither has minted yet) could
        # each independently conclude "CODE" is free and collide.
        #
        # With the placeholders in agreement, EITHER sibling seeing the
        # other's not-yet-real "CODE" placeholder is enough to make it
        # defensively extend past the short prefix -- unlike the arb-prd/
        # arb-prod case (where the first minter keeps the short prefix
        # cleanly because the second hasn't registered a same-shaped
        # placeholder yet), here neither has minted, so it's not knowable in
        # advance which one "should" get to keep "CODE". Collision-safety
        # (the actual invariant under test), not who keeps the short prefix,
        # is what this test asserts.
        _make_portfolio(tmp_path, monkeypatch, "code-quorum")
        _add_project(tmp_path, "code-runner")  # also task-less at this point
        first = _add("code-quorum", "e")
        second = _add("code-runner", "e")
        pre = lambda tid: tid.rsplit("-", 1)[0]
        assert pre(second) != pre(first), (first, second)  # distinct namespaces
        assert len({first, second}) == 2, (first, second)  # no literal id collision

    def test_two_taskless_siblings_with_deep_collision_do_not_converge_on_the_same_extension(
        self, tmp_path, monkeypatch
    ):
        # CLAWP-121 (grok-4.6 repro, PR #57 round-17 fallout): "code-quorum"
        # and "code-quiz" both reduce to base "CODE" -- AND their first
        # extension also collides (both slice to "CODE-Q" at n=6). The
        # previous test above ("code-quorum"/"code-runner") only diverges at
        # n=6 ("CODE-Q" vs "CODE-R"), so it never exercised this.
        #
        # `_portfolio_prefixes` used to reserve only a task-less sibling's
        # 5-char placeholder ("CODE"), not the full chain of extended
        # candidates that sibling could still grow into. doctor's per-project
        # loop calls `assign_task_prefix` independently for each task-less
        # project (neither has minted, so neither sees the other's REAL
        # resolution) -- so both calls saw only "CODE" reserved and both
        # independently extended past it to the SAME "CODE-Q".
        _make_portfolio(tmp_path, monkeypatch, "code-quorum")
        _add_project(tmp_path, "code-quiz")  # also task-less at this point

        from clawpm.discovery import load_portfolio_config
        from clawpm.tasks import assign_task_prefix

        config = load_portfolio_config(tmp_path)
        # Independent calls, mirroring doctor's per-project loop -- neither
        # persists a mint, so both see an identical task-less portfolio.
        a = assign_task_prefix(
            "code-quorum",
            tmp_path / "projects" / "code-quorum" / ".project" / "tasks",
            config,
        )
        b = assign_task_prefix(
            "code-quiz",
            tmp_path / "projects" / "code-quiz" / ".project" / "tasks",
            config,
        )
        assert a != b, (a, b)  # the actual id-uniqueness invariant under test

    def test_taskless_sibling_whose_id_is_a_literal_prefix_does_not_starve_the_shorter_project(
        self, tmp_path, monkeypatch
    ):
        # CLAWP-121 round 2 (code-reviewer + history-lens PRE-REVIEW, PR #60):
        # the FIRST fix (reserving a task-less sibling's full extension
        # chain, above) over-corrected. When one sibling's id is a literal
        # prefix of another's ("clawpm" / "clawpm-extra" -- this repo's own
        # naming pattern), the LONGER sibling's full chain contains the
        # SHORTER project's own full id as one of its entries. Reserving
        # that unconditionally left the shorter project with NO free
        # candidate at all -- a spurious ValueError -- even though the
        # shorter project has no room to move and the longer one does.
        #
        # Fix: a sibling's chain reservation is capped at the EXCLUDING
        # project's own full length, so a project always keeps a candidate
        # at its own maximum length uncontested.
        _make_portfolio(tmp_path, monkeypatch, "clawpm")
        _add_project(tmp_path, "clawpm-extra")  # literal-prefix sibling, task-less

        from clawpm.discovery import load_portfolio_config
        from clawpm.tasks import assign_task_prefix

        config = load_portfolio_config(tmp_path)
        short = assign_task_prefix(
            "clawpm", tmp_path / "projects" / "clawpm" / ".project" / "tasks", config,
        )
        long_ = assign_task_prefix(
            "clawpm-extra",
            tmp_path / "projects" / "clawpm-extra" / ".project" / "tasks",
            config,
        )
        assert short is not None  # must not raise/refuse -- a real candidate exists
        assert short != long_, (short, long_)


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

    def test_explicit_sibling_prefixes_exhaust_the_candidates_and_raise(
        self, tmp_path, monkeypatch
    ):
        # Codex P1, PR #57: the last resort used to `return full`, which
        # assumed the unstripped full id can't collide because ids are
        # portfolio-unique -- true for id-DERIVED prefixes, false for
        # EXPLICIT ones, which are arbitrary strings a sibling can set
        # independent of its own id. Two siblings with explicit prefixes
        # "ABCDE" and "ABCDE-F" exhaust every stripped candidate a new
        # "abcde-f" project would try (base "ABCDE", extension "ABCDE-F"),
        # leaving the final fallback `full` == "ABCDE-F" ALREADY claimed.
        #
        # There is no synthesised candidate to fall back to (CLAWP-119), so
        # the allocator refuses rather than minting a claimed prefix. The
        # message must name the remedy: an explicit `task_prefix`.
        from clawpm.discovery import load_portfolio_config
        from clawpm.tasks import assign_task_prefix

        tasks_dir = _make_portfolio(tmp_path, monkeypatch, "abcde-f")
        _add_project(tmp_path, "sib-one", task_prefix="ABCDE")
        _add_project(tmp_path, "sib-two", task_prefix="ABCDE-F")

        config = load_portfolio_config(tmp_path)
        with pytest.raises(ValueError, match="task_prefix"):
            assign_task_prefix("abcde-f", tasks_dir, config)

    def test_trailing_separator_twins_never_mint_a_doubled_separator(
        self, tmp_path, monkeypatch
    ):
        # Codex P2, PR #57: the pre-CLAWP-096 last resort returned the
        # UNSTRIPPED `full`, so project "code-" whose base "CODE" is claimed
        # by a sibling minted "CODE--000" -- the doubled separator CLAWP-096
        # exists to remove, which inference then pinned.
        #
        # Refusing satisfies this outright: nothing is minted, so nothing
        # carries a doubled separator. Two ids differing only in trailing
        # separators ("code-" / "code---") also cannot collapse onto one
        # prefix, because neither produces one.
        from clawpm.discovery import load_portfolio_config
        from clawpm.tasks import assign_task_prefix

        tasks_a = _make_portfolio(tmp_path, monkeypatch, "code-")
        _add_project(tmp_path, "sib-one", task_prefix="CODE")
        _add_project(tmp_path, "code---")
        tasks_b = tmp_path / "projects" / "code---" / ".project" / "tasks"

        config = load_portfolio_config(tmp_path)
        for project_id, tasks_dir in (("code-", tasks_a), ("code---", tasks_b)):
            with pytest.raises(ValueError, match="collision-free task prefix"):
                assign_task_prefix(project_id, tasks_dir, config)

    def test_concurrent_first_mints_cannot_select_the_same_candidate(
        self, tmp_path, monkeypatch
    ):
        """Codex P1, PR #57 round 4: concurrent FIRST mints must not collide.

        The fallback of the day picked its suffix with
        ``while f"{stem}{n}" in used: n += 1`` — a scan of a portfolio
        snapshot. Nothing pins that choice until the task file is written,
        and ``add_task`` locks per-project task dirs, so two task-less twin
        projects minting concurrently both saw the same snapshot and both
        selected ``CODE2``.

        That round was closed by making the candidate a pure function of the
        project id; CLAWP-119 removes the synthesised candidate altogether,
        which closes it more directly — there is no selected value left for
        two callers to converge on. This test keeps the ROUND-4 PROPERTY
        (both callers see identical pre-mint state and neither ends up with
        the other's prefix) rather than the mechanism that satisfied it.
        """
        from clawpm.discovery import load_portfolio_config
        from clawpm.tasks import assign_task_prefix

        tasks_a = _make_portfolio(tmp_path, monkeypatch, "code-")
        _add_project(tmp_path, "sib-one", task_prefix="CODE")
        _add_project(tmp_path, "code---")
        tasks_b = tmp_path / "projects" / "code---" / ".project" / "tasks"

        config = load_portfolio_config(tmp_path)
        # Neither project has minted yet: both calls see identical state.
        for project_id, tasks_dir in (("code-", tasks_a), ("code---", tasks_b)):
            with pytest.raises(ValueError):
                assign_task_prefix(project_id, tasks_dir, config)


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

    def test_doctor_keys_a_taskless_sibling_under_what_the_allocator_mints(
        self, tmp_path, monkeypatch
    ):
        # CLAWP-096 (grok review, cli/project.py): doctor's collision map must
        # key a still-task-less sibling under the prefix that project will
        # ACTUALLY get, not a bare unstripped `id.upper()[:5]`.
        #
        # Codex P2, PR #57 round 6 corrected what "actually get" means. This
        # test previously asserted a CODE collision between the two, on the
        # premise that a task-less sibling derives the naive base. It does
        # not: the naive base is only the allocator's FIRST candidate, and
        # `assign_task_prefix` sees code-quorum's minted CODE in `used` and
        # extends to CODE-R. Reporting CODE was a false positive — and since
        # `prefix_collisions` feeds `has_warnings`, it failed `doctor
        # --strict` in CI over a namespace nothing would ever mint.
        _make_portfolio(tmp_path, monkeypatch, "code-quorum")
        (tmp_path / "projects" / "code-quorum" / ".project" / "tasks" / "CODE-000.md").write_text(
            "---\nid: CODE-000\n---\n", encoding="utf-8"
        )
        _add_project(tmp_path, "code-runner")  # task-less

        # What the allocator really mints for the task-less sibling.
        from clawpm.discovery import load_portfolio_config
        from clawpm.tasks import assign_task_prefix

        config = load_portfolio_config(tmp_path)
        minted = assign_task_prefix(
            "code-runner",
            tmp_path / "projects" / "code-runner" / ".project" / "tasks",
            config,
        )
        assert minted == "CODE-R", minted

        res = CliRunner().invoke(main, ["--format", "json", "doctor"])
        cols = self._prefix_collisions(res.output)
        assert cols is not None, res.output
        assert not any(
            c["prefix"] == "CODE" and "code-runner" in c["projects"] for c in cols
        ), (
            "code-runner never mints under CODE — reporting it as a collision "
            "fails doctor --strict and tells the operator to rename a project "
            "that needs no rename"
        )
        # And it must not be dropped from the check either: whatever prefix it
        # is keyed under, a genuine clash on THAT prefix still has to surface.
        assert not any(
            c["prefix"] == minted and len(c["projects"]) > 1 for c in cols
        ), cols

    def test_doctor_surfaces_allocator_refusal_instead_of_swallowing_it(
        self, tmp_path, monkeypatch
    ):
        """CLAWP-119 fallout (antigravity/grok-4.5/grok-4.6, PR #57).

        doctor's cross-project collision check used to catch a bare
        ``except Exception`` around the allocator call and fall back to the
        naive placeholder with no trace anywhere -- a diagnostic command
        silently hiding a real, actionable refusal is exactly the failure
        mode `expired_lease_findings` next to it already guards against.
        Same scenario as
        ``test_explicit_sibling_prefixes_exhaust_the_candidates_and_raise``
        (two explicit sibling prefixes exhaust every id-derived candidate),
        but exercised through `doctor` rather than `assign_task_prefix`
        directly, to prove the CLI-facing surface actually reports it.

        CLAWP-120 PRE-REVIEW (this round): the naive-placeholder fallback
        this test originally pinned was itself a false-collision bug --
        identical in shape to the one fixed above for the resolved-prefix
        path, since a ValueError refusal means the naive base is NECESSARILY
        claimed by whichever sibling caused the refusal (here: sib-one's
        explicit ABCDE). Reporting that as a `prefix_collisions` entry tells
        the operator sib-one needs renaming too, when only abcde-f does.
        `prefix_map` has no reader besides `prefix_collisions`, so nothing
        is lost by NOT keying the refused project into it -- the issues[]
        entry alone is the actionable surface.
        """
        _make_portfolio(tmp_path, monkeypatch, "abcde-f")
        _add_project(tmp_path, "sib-one", task_prefix="ABCDE")
        _add_project(tmp_path, "sib-two", task_prefix="ABCDE-F")

        res = CliRunner().invoke(main, ["--format", "json", "doctor"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert any(
            i["scope"] == "prefix" and "abcde-f" in i["message"]
            for i in data.get("issues", [])
        ), data.get("issues")
        # It must NOT appear in the collision map: there is no real prefix
        # to key it under, and the naive-base fallback manufactures a
        # collision against sib-one's perfectly valid ABCDE.
        cols = self._prefix_collisions(res.output)
        assert not any("abcde-f" in c["projects"] for c in cols), cols
        # sib-one/sib-two's own explicit prefixes must still resolve clean.
        assert not any(
            c["prefix"] in ("ABCDE", "ABCDE-F") for c in cols
        ), cols

    def test_doctor_surfaces_unreadable_sibling_task_dir_instead_of_aborting(
        self, tmp_path, monkeypatch
    ):
        """CLAWP-120 PRE-REVIEW: the allocator's own exception-handling arm in
        doctor's collision check only caught ``ValueError`` (CLAWP-119
        refusal). But `assign_task_prefix` -> `_portfolio_prefixes` ->
        `resolve_existing_prefix` -> `_infer_prefix_from_tasks` does a raw
        ``Path.iterdir()`` with no exception handling at all -- an unreadable
        directory (Windows AV lock, a concurrent clawpm session, a broken
        symlink) raised `OSError` straight out of `project_doctor`, aborting
        `doctor` for the ENTIRE portfolio over one project's transient scan
        failure. Mirrors the lease-scanning block a few lines below, which
        already declares its blind spots (`except Exception` -> issues[]
        warning) rather than crashing the whole command.

        Patches `resolve_existing_prefix` itself (not the filesystem) so this
        exercises ONLY the collision-check block this round actually
        touches -- `project_doctor` has an unrelated, pre-existing unguarded
        `Path.iterdir()` in `list_tasks` (:508, `_scan_task_files`) that
        would swallow a filesystem-level OSError before ever reaching this
        code, which is a real but separately-tracked gap (CLAWP-094:
        "harden fail-open error handling ... discovery/context/research/
        doctor"), not part of this fix's scope.
        """
        import clawpm.tasks as _tasks_mod

        _make_portfolio(tmp_path, monkeypatch, "victim")
        _add_project(tmp_path, "locked-sib")
        real_resolve = _tasks_mod.resolve_existing_prefix

        def _raising_resolve(settings):
            if getattr(settings, "id", None) == "locked-sib":
                raise OSError(13, "Permission denied", "locked-sib/.project/tasks")
            return real_resolve(settings)

        monkeypatch.setattr(_tasks_mod, "resolve_existing_prefix", _raising_resolve)

        res = CliRunner().invoke(main, ["--format", "json", "doctor"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        # errno 13 auto-promotes OSError to PermissionError (a subclass), so
        # check the actual raised type rather than the literal base class name.
        assert any(
            i["scope"] == "prefix" and "locked-sib" in i["message"]
            and "PermissionError" in i["message"]
            for i in data.get("issues", [])
        ), data.get("issues")
        # The command must complete and still report on the OTHER project.
        cols = self._prefix_collisions(res.output)
        assert not any("locked-sib" in c["projects"] for c in cols), cols
        # grok-4.5, PR #57 round: the assertion above is satisfied by
        # locked-sib's own resolve turn (`_resolve_prefix(locked-sib)` at
        # project.py:786, which correctly names itself) REGARDLESS of
        # whether the sibling-scan attribution fix below exists -- it
        # doesn't actually exercise the fix. Isolate the sibling-scan path
        # specifically: no issue may misattribute the failure to `victim`
        # (the taskless project whose OWN mint triggered the scan), and the
        # PortfolioPrefixScanError wording must appear for the real sibling.
        assert not any(
            i["scope"] == "prefix" and i["message"].startswith("victim:")
            for i in data.get("issues", [])
        ), data.get("issues")
        assert any(
            i["scope"] == "prefix"
            and "could not evaluate prefix collisions for sibling 'locked-sib'" in i["message"]
            for i in data.get("issues", [])
        ), data.get("issues")


# ---------------------------------------------------------------------------
# CLAWP-098 predecessor (Codex P2, PR #55 round 11, discovery.py:259): a
# registered worktree's own committed task_prefix must be honoured, not the
# canonical checkout's. `get_tasks_dir` already redirects the task STORE
# into the worktree; `add_task` separately resolved settings via the
# cwd-independent `get_project(...)`, so a worktree whose own settings.toml
# set a different task_prefix still minted IDs under the canonical
# checkout's prefix, risking a collision when the branch merges.
# ---------------------------------------------------------------------------


class TestSessionScopedTaskPrefix:
    def test_worktree_own_task_prefix_is_used_when_session_active(
        self, isolated_portfolio, tmp_path, monkeypatch
    ):
        from clawpm.sessions import register_session
        from clawpm.tasks import add_task

        # Worktree carries its OWN .project/ with a task_prefix the
        # canonical checkout does not set.
        wt = tmp_path / "wt"
        wt_tasks = wt / ".project" / "tasks"
        for sub in ("done", "blocked"):
            (wt_tasks / sub).mkdir(parents=True)
        (wt / ".project" / "settings.toml").write_text(
            'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
            'task_prefix = "WTPFX"\n',
            encoding="utf-8",
        )

        register_session(
            isolated_portfolio.root, "sess-1", "SEED",
            isolated_portfolio.project_id, wt,
        )
        monkeypatch.chdir(wt)

        task = add_task(
            isolated_portfolio.config, isolated_portfolio.project_id,
            "from worktree",
        )
        assert task is not None
        # Canonical checkout has no explicit task_prefix, so the pre-fix
        # cwd-independent lookup derived "TEST" from the project id instead.
        assert task.id.startswith("WTPFX-"), task.id
        # And it must have landed in the worktree's own task store.
        assert (wt_tasks / f"{task.id}.md").exists()

    def test_worktree_settings_with_foreign_id_fails_closed(
        self, isolated_portfolio, tmp_path, monkeypatch
    ):
        """`ProjectSettings.load` does no id validation, unlike the
        registry's `get_project` (which only ever returns settings whose
        `id == project_id`). A worktree registered for THIS project but
        whose committed settings.toml carries a DIFFERENT project's id
        must not have that foreign task_prefix used as an explicit
        override — that would bypass assign_task_prefix's portfolio-wide
        collision check entirely (the cross-project prefix-collision
        class CLAWP-048 already exists to prevent, reopened via a new
        route). Rounds 11-12 silently fell back to the canonical settings;
        by operator decision (2026-09-21) it now FAILS CLOSED, loudly."""
        from clawpm.discovery import ScopedSettingsMismatchError
        from clawpm.sessions import register_session
        from clawpm.tasks import add_task

        wt = tmp_path / "wt"
        wt_tasks = wt / ".project" / "tasks"
        for sub in ("done", "blocked"):
            (wt_tasks / sub).mkdir(parents=True)
        # Registered for "test", but its OWN settings.toml claims a
        # different project id and prefix.
        (wt / ".project" / "settings.toml").write_text(
            'id = "other-project"\nname = "Other"\nstatus = "active"\n'
            'priority = 3\ntask_prefix = "FOREIGN"\n',
            encoding="utf-8",
        )

        register_session(
            isolated_portfolio.root, "sess-1", "SEED",
            isolated_portfolio.project_id, wt,
        )
        monkeypatch.chdir(wt)

        with pytest.raises(ScopedSettingsMismatchError, match="other-project"):
            add_task(
                isolated_portfolio.config, isolated_portfolio.project_id,
                "from worktree with foreign id",
            )
        assert not list(wt_tasks.glob("*.md"))
