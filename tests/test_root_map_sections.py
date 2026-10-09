"""Tests for CLAWP-111-002 — "A root task is a map".

Covers:
  1. Task.destination / Task.not_yet_specified — schema, parsing, omit-when-
     default persistence, to_dict exposure.
  2. render_task_map_sections (output.py) — the ## Destination / ## Decisions
     so far / ## Not yet specified / ## Out of scope renderer used by
     `tasks show`, fixed order, absent-field-renders-nothing.
  3. `tasks add --parent <root> --graduates "<text>"` — exact/prefix match
     (case-insensitive), zero/multiple-match error creates nothing.
  4. `tasks fog <root> --add/--drop` — isolated mutator, diff-clean.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.models import Task, TaskState
from clawpm.output import render_task_map_sections
from clawpm.tasks import (
    FogMatchError,
    add_subtask,
    add_task,
    edit_fog,
    get_task,
)


# ---------------------------------------------------------------------------
# 1. Schema + persistence
# ---------------------------------------------------------------------------

class TestSchemaAndPersistence:
    def test_defaults_are_absent(self, isolated_portfolio):
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Plain task")
        assert task is not None
        assert task.destination is None
        assert task.not_yet_specified == []

    def test_no_frontmatter_diff_when_absent(self, isolated_portfolio):
        """A task created without destination/not_yet_specified never writes
        those keys to disk — no frontmatter diff on existing fixtures."""
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Plain task")
        assert task is not None and task.file_path is not None
        raw = task.file_path.read_text(encoding="utf-8")
        assert "destination" not in raw
        assert "not_yet_specified" not in raw

    def test_round_trip_from_frontmatter(self, isolated_portfolio):
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Map task")
        assert task is not None and task.file_path is not None
        raw = task.file_path.read_text(encoding="utf-8")
        # Splice destination + not_yet_specified into the frontmatter directly
        # (no CLI setter exists for these — deliberately out of scope; the
        # fog list's only supported write paths are `tasks fog` and
        # `--graduates`, exercised elsewhere in this file).
        assert raw.startswith("---\n")
        head, _, rest = raw.partition("---\n")
        fm_text, _, body = rest.partition("\n---\n")
        new_fm = fm_text + "\ndestination: Ship v2 to prod\nnot_yet_specified:\n- pricing model\n- rollout plan\n"
        task.file_path.write_text(f"---\n{new_fm}\n---\n{body}", encoding="utf-8")

        reloaded = get_task(isolated_portfolio.config, isolated_portfolio.project_id, task.id)
        assert reloaded is not None
        assert reloaded.destination == "Ship v2 to prod"
        assert reloaded.not_yet_specified == ["pricing model", "rollout plan"]

    def test_malformed_frontmatter_degrades_to_defaults(self, isolated_portfolio):
        """Wrong-typed frontmatter (e.g. destination as a list, not_yet_specified
        as a string) degrades leniently rather than raising, matching the other
        CLAWP-054 contract fields."""
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Bad map task")
        assert task is not None and task.file_path is not None
        raw = task.file_path.read_text(encoding="utf-8")
        head, _, rest = raw.partition("---\n")
        fm_text, _, body = rest.partition("\n---\n")
        new_fm = fm_text + "\ndestination:\n- not a string\nnot_yet_specified: not a list\n"
        task.file_path.write_text(f"---\n{new_fm}\n---\n{body}", encoding="utf-8")

        reloaded = get_task(isolated_portfolio.config, isolated_portfolio.project_id, task.id)
        assert reloaded is not None
        assert reloaded.destination is None
        assert reloaded.not_yet_specified == []

    def test_to_dict_exposes_fields(self, isolated_portfolio):
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Dict task")
        assert task is not None
        d = task.to_dict()
        assert d["destination"] is None
        assert d["not_yet_specified"] == []


# ---------------------------------------------------------------------------
# 2. render_task_map_sections
# ---------------------------------------------------------------------------

def _task(**kwargs) -> Task:
    defaults = dict(id="T-001", title="Test", state=TaskState.OPEN)
    defaults.update(kwargs)
    return Task(**defaults)


class TestRenderTaskMapSections:
    def test_all_absent_renders_nothing(self):
        assert render_task_map_sections(_task()) == ""

    def test_destination_only(self):
        out = render_task_map_sections(_task(destination="Ship v2 to prod"))
        assert out == "## Destination\n\nShip v2 to prod"

    def test_not_yet_specified_only(self):
        out = render_task_map_sections(_task(not_yet_specified=["pricing", "rollout"]))
        assert out == "## Not yet specified\n\n- pricing\n- rollout"

    def test_out_of_scope_only(self):
        out = render_task_map_sections(_task(out_of_scope=["docs/**", "legacy auth"]))
        assert out == "## Out of scope\n\n- docs/**\n- legacy auth"

    def test_decisions_so_far_extracted_from_body(self):
        content = (
            "# Test\n\nIntro text.\n\n## Decisions so far\n\n"
            "- [Child](T-001-001): went with option B\n\n## Notes\n\nsome notes\n"
        )
        out = render_task_map_sections(_task(content=content))
        assert out == "## Decisions so far\n\n- [Child](T-001-001): went with option B"

    def test_decisions_so_far_heading_case_insensitive(self):
        content = "# Test\n\n## decisions SO far\n\n- one decision\n"
        out = render_task_map_sections(_task(content=content))
        assert out == "## Decisions so far\n\n- one decision"

    def test_decisions_so_far_absent_when_no_heading(self):
        content = "# Test\n\n## Notes\n\nsome notes\n"
        assert render_task_map_sections(_task(content=content)) == ""

    def test_decisions_so_far_absent_when_section_empty(self):
        content = "# Test\n\n## Decisions so far\n\n## Notes\n\nsome notes\n"
        assert render_task_map_sections(_task(content=content)) == ""

    def test_decisions_so_far_runs_to_end_of_content(self):
        content = "# Test\n\n## Decisions so far\n\n- only decision\n"
        out = render_task_map_sections(_task(content=content))
        assert out == "## Decisions so far\n\n- only decision"

    def test_all_four_present_render_in_fixed_order(self):
        content = "# Test\n\n## Decisions so far\n\n- decided X\n\n## Notes\n\nfiller\n"
        task = _task(
            destination="Ship v2",
            content=content,
            not_yet_specified=["pricing"],
            out_of_scope=["legacy auth"],
        )
        out = render_task_map_sections(task)
        headings = ["## Destination", "## Decisions so far", "## Not yet specified", "## Out of scope"]
        positions = [out.index(h) for h in headings]
        assert positions == sorted(positions), f"headings out of order: {out!r}"
        # Every heading present exactly once.
        for h in headings:
            assert out.count(h) == 1

    def test_partial_combination_skips_absent_sections_only(self):
        """Destination + Out of scope present, Decisions/Not-yet-specified
        absent — only the present two render, still in relative order."""
        task = _task(destination="Ship v2", out_of_scope=["legacy auth"])
        out = render_task_map_sections(task)
        assert "## Decisions so far" not in out
        assert "## Not yet specified" not in out
        assert out.index("## Destination") < out.index("## Out of scope")

    def test_content_none_does_not_raise(self):
        # Task.content defaults to "" — exercised directly to guard the
        # getattr(..., "") fallback in the renderer.
        out = render_task_map_sections(_task(content=""))
        assert out == ""


# ---------------------------------------------------------------------------
# 3. --graduates
# ---------------------------------------------------------------------------

def _make_root_with_fog(portfolio, entries: list[str]) -> Task:
    root = add_task(portfolio.config, portfolio.project_id, "Root map")
    assert root is not None
    for e in entries:
        edit_fog(portfolio.config, portfolio.project_id, root.id, add=e)
    reloaded = get_task(portfolio.config, portfolio.project_id, root.id)
    assert reloaded is not None
    assert reloaded.not_yet_specified == entries
    return reloaded


class TestGraduatesFunction:
    def test_exact_match_graduates(self, isolated_portfolio):
        root = _make_root_with_fog(isolated_portfolio, ["ship the release", "pricing model"])
        child = add_subtask(
            isolated_portfolio.config, isolated_portfolio.project_id, root.id,
            "Ship it", graduates="ship the release",
        )
        assert child is not None
        assert child.parent == root.id

        reloaded_root = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert reloaded_root.not_yet_specified == ["pricing model"]
        assert child.id in reloaded_root.children

    def test_case_insensitive_prefix_match_graduates(self, isolated_portfolio):
        root = _make_root_with_fog(isolated_portfolio, ["Ship the release", "Pricing model"])
        child = add_subtask(
            isolated_portfolio.config, isolated_portfolio.project_id, root.id,
            "Price it", graduates="pricing",
        )
        assert child is not None
        reloaded_root = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert reloaded_root.not_yet_specified == ["Ship the release"]

    def test_exact_match_wins_over_prefix_ambiguity(self, isolated_portfolio):
        """'ship' exactly matches 'ship' even though 'shipping plan' would also
        prefix-match a case-insensitive 'ship' query — exact tier resolves
        first and short-circuits the prefix tier entirely."""
        root = _make_root_with_fog(isolated_portfolio, ["ship", "shipping plan"])
        child = add_subtask(
            isolated_portfolio.config, isolated_portfolio.project_id, root.id,
            "Ship", graduates="ship",
        )
        assert child is not None
        reloaded_root = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert reloaded_root.not_yet_specified == ["shipping plan"]

    def test_zero_match_creates_nothing(self, isolated_portfolio):
        root = _make_root_with_fog(isolated_portfolio, ["pricing model"])
        before_children = list(root.children)

        with pytest.raises(FogMatchError) as exc_info:
            add_subtask(
                isolated_portfolio.config, isolated_portfolio.project_id, root.id,
                "Nope", graduates="nonexistent text",
            )
        assert exc_info.value.candidates == []

        reloaded_root = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert reloaded_root.not_yet_specified == ["pricing model"]
        assert reloaded_root.children == before_children

    def test_multiple_prefix_matches_creates_nothing(self, isolated_portfolio):
        root = _make_root_with_fog(isolated_portfolio, ["ship v1", "ship v2"])
        before_children = list(root.children)

        with pytest.raises(FogMatchError) as exc_info:
            add_subtask(
                isolated_portfolio.config, isolated_portfolio.project_id, root.id,
                "Ambiguous", graduates="ship",
            )
        assert set(exc_info.value.candidates) == {"ship v1", "ship v2"}

        reloaded_root = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert reloaded_root.not_yet_specified == ["ship v1", "ship v2"]
        assert reloaded_root.children == before_children

    def test_multiple_exact_case_insensitive_matches_creates_nothing(self, isolated_portfolio):
        root = _make_root_with_fog(isolated_portfolio, ["Ship", "ship"])
        with pytest.raises(FogMatchError) as exc_info:
            add_subtask(
                isolated_portfolio.config, isolated_portfolio.project_id, root.id,
                "Ambiguous", graduates="SHIP",
            )
        assert set(exc_info.value.candidates) == {"Ship", "ship"}
        reloaded_root = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert reloaded_root.not_yet_specified == ["Ship", "ship"]

    def test_literal_exact_beats_case_insensitive_duplicate(self, isolated_portfolio):
        """fog ["Ship", "ship"] + --graduates "ship": the literal exact entry
        wins; case-insensitive equality is only a fallback tier."""
        root = _make_root_with_fog(isolated_portfolio, ["Ship", "ship"])
        child = add_subtask(
            isolated_portfolio.config, isolated_portfolio.project_id, root.id,
            "Ship", graduates="ship",
        )
        assert child is not None
        reloaded_root = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert reloaded_root.not_yet_specified == ["Ship"]

    def test_parent_commit_failure_rolls_back_child_and_retry_is_clean(
        self, isolated_portfolio, monkeypatch,
    ):
        """If the parent rename fails after the child was committed, the child
        must not be left behind; a retry then creates exactly one child."""
        from pathlib import Path

        import clawpm.concurrency as concurrency

        root = _make_root_with_fog(isolated_portfolio, ["pricing model"])
        real = concurrency.retry_transient

        def flaky(fn, *args, **kwargs):
            if args and Path(args[0]).name == "_task.md":
                raise OSError("injected parent rename failure")
            return real(fn, *args, **kwargs)

        monkeypatch.setattr(concurrency, "retry_transient", flaky)
        with pytest.raises(OSError, match="injected"):
            add_subtask(
                isolated_portfolio.config, isolated_portfolio.project_id, root.id,
                "Price it", graduates="pricing",
            )
        monkeypatch.setattr(concurrency, "retry_transient", real)

        task_dir = get_task(
            isolated_portfolio.config, isolated_portfolio.project_id, root.id,
        ).file_path.parent
        assert [p.name for p in task_dir.glob(f"{root.id}-*.md")] == []
        still = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert still.not_yet_specified == ["pricing model"]

        child = add_subtask(
            isolated_portfolio.config, isolated_portfolio.project_id, root.id,
            "Price it", graduates="pricing",
        )
        assert child is not None
        assert len(list(task_dir.glob(f"{root.id}-*.md"))) == 1
        final = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert final.not_yet_specified == []
        assert final.children == [child.id]

    def test_no_child_file_left_behind_on_failed_match(self, isolated_portfolio):
        root = _make_root_with_fog(isolated_portfolio, ["pricing model"])
        root_dir = root.file_path.parent  # split into a directory by _make_root_with_fog's edit_fog calls? no -- edit_fog doesn't split.
        # add_subtask WILL split the parent into a directory as part of its own
        # flow, but only after the graduates match succeeds — confirm the
        # parent file is untouched (still a flat .md, not split) on failure.
        assert root.file_path.name != "_task.md"

        with pytest.raises(FogMatchError):
            add_subtask(
                isolated_portfolio.config, isolated_portfolio.project_id, root.id,
                "Nope", graduates="nonexistent",
            )

        reloaded_root = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        # Parent was NOT split into a directory task — zero mutation on failure,
        # including the split itself.
        assert reloaded_root.file_path.name != "_task.md"


class TestGraduatesCLI:
    def test_cli_graduates_single_match(self, isolated_portfolio):
        root = _make_root_with_fog(isolated_portfolio, ["ship the release", "pricing model"])
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["tasks", "add", "--title", "Ship it", "--project", isolated_portfolio.project_id,
             "--parent", root.id, "--graduates", "ship the release"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["parent"] == root.id

        reloaded_root = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert reloaded_root.not_yet_specified == ["pricing model"]

    def test_cli_graduates_zero_match_is_clean_error(self, isolated_portfolio):
        root = _make_root_with_fog(isolated_portfolio, ["pricing model"])
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["tasks", "add", "--title", "Nope", "--project", isolated_portfolio.project_id,
             "--parent", root.id, "--graduates", "nonexistent"],
        )
        assert result.exit_code == 1
        assert not isinstance(result.exception, FogMatchError), (
            f"FogMatchError leaked instead of a clean error: {result.exception!r}"
        )
        err = json.loads(result.output)
        assert err["error"] == "add_failed"

        reloaded_root = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert reloaded_root.not_yet_specified == ["pricing model"]
        assert reloaded_root.children == []

    def test_cli_graduates_multiple_match_is_clean_error(self, isolated_portfolio):
        root = _make_root_with_fog(isolated_portfolio, ["ship v1", "ship v2"])
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["tasks", "add", "--title", "Ambiguous", "--project", isolated_portfolio.project_id,
             "--parent", root.id, "--graduates", "ship"],
        )
        assert result.exit_code == 1
        err = json.loads(result.output)
        assert "ship v1" in err["message"] and "ship v2" in err["message"]

        reloaded_root = get_task(isolated_portfolio.config, isolated_portfolio.project_id, root.id)
        assert reloaded_root.not_yet_specified == ["ship v1", "ship v2"]
        assert reloaded_root.children == []

    def test_cli_graduates_without_parent_is_usage_error(self, isolated_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["tasks", "add", "--title", "Orphan", "--project", isolated_portfolio.project_id,
             "--graduates", "anything"],
        )
        assert result.exit_code != 0
        assert "--graduates requires --parent" in result.output


# ---------------------------------------------------------------------------
# 4. `tasks fog --add/--drop`
# ---------------------------------------------------------------------------

class TestFogCommand:
    def test_add_creates_fog_list(self, isolated_portfolio):
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Fog task")
        assert task is not None
        updated = edit_fog(isolated_portfolio.config, isolated_portfolio.project_id, task.id, add="pricing model")
        assert updated is not None
        assert updated.not_yet_specified == ["pricing model"]

    def test_add_is_idempotent(self, isolated_portfolio):
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Fog task")
        edit_fog(isolated_portfolio.config, isolated_portfolio.project_id, task.id, add="pricing model")
        updated = edit_fog(isolated_portfolio.config, isolated_portfolio.project_id, task.id, add="pricing model")
        assert updated.not_yet_specified == ["pricing model"]

    def test_drop_removes_entry(self, isolated_portfolio):
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Fog task")
        edit_fog(isolated_portfolio.config, isolated_portfolio.project_id, task.id, add="pricing model")
        edit_fog(isolated_portfolio.config, isolated_portfolio.project_id, task.id, add="rollout plan")
        updated = edit_fog(isolated_portfolio.config, isolated_portfolio.project_id, task.id, drop="pricing model")
        assert updated.not_yet_specified == ["rollout plan"]

    def test_drop_nonexistent_entry_is_noop_on_list(self, isolated_portfolio):
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Fog task")
        edit_fog(isolated_portfolio.config, isolated_portfolio.project_id, task.id, add="pricing model")
        updated = edit_fog(isolated_portfolio.config, isolated_portfolio.project_id, task.id, drop="nonexistent")
        assert updated.not_yet_specified == ["pricing model"]

    def test_drop_last_entry_removes_key_entirely(self, isolated_portfolio):
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Fog task")
        edit_fog(isolated_portfolio.config, isolated_portfolio.project_id, task.id, add="pricing model")
        edit_fog(isolated_portfolio.config, isolated_portfolio.project_id, task.id, drop="pricing model")
        raw = task.file_path.read_text(encoding="utf-8")
        assert "not_yet_specified" not in raw

    def test_diff_touches_only_fog_and_updated_stamp(self, isolated_portfolio):
        """The core safety property: --add/--drop change nothing else in the
        file — no incidental rewrite of unrelated frontmatter or body."""
        import yaml
        from clawpm.frontmatter import split_frontmatter

        task = add_task(
            isolated_portfolio.config, isolated_portfolio.project_id, "Fog task",
            priority=2, scope=["src/**"], tags=["alpha"],
        )
        assert task is not None and task.file_path is not None
        before_raw = task.file_path.read_text(encoding="utf-8")
        before_fm, before_body = split_frontmatter(before_raw)

        edit_fog(isolated_portfolio.config, isolated_portfolio.project_id, task.id, add="pricing model")

        after_raw = task.file_path.read_text(encoding="utf-8")
        after_fm, after_body = split_frontmatter(after_raw)

        assert after_body == before_body

        before_fm.pop("not_yet_specified", None)
        before_fm.pop("updated", None)
        after_fm.pop("not_yet_specified", None)
        after_fm.pop("updated", None)
        assert after_fm == before_fm

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    def test_bom_prefixed_file_keeps_frontmatter(self, isolated_portfolio, newline):
        """A UTF-8 BOM (and CRLF) must not make the frontmatter look absent:
        existing metadata survives, only the fog change + updated stamp differ,
        and the BOM / line endings are preserved."""
        from clawpm.frontmatter import split_frontmatter

        task = add_task(
            isolated_portfolio.config, isolated_portfolio.project_id, "Fog task",
            priority=2, scope=["src/**"], tags=["alpha"],
        )
        assert task is not None and task.file_path is not None
        clean = task.file_path.read_bytes().decode("utf-8").replace("\r\n", "\n")
        before_fm, before_body = split_frontmatter(clean)
        task.file_path.write_bytes(
            b"\xef\xbb\xbf" + clean.replace("\n", newline).encode("utf-8")
        )

        updated = edit_fog(
            isolated_portfolio.config, isolated_portfolio.project_id, task.id,
            add="pricing model",
        )
        assert updated is not None

        raw = task.file_path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf---")
        text = raw[3:].decode("utf-8")
        if newline == "\r\n":
            assert "\n" not in text.replace("\r\n", "")
        after_fm, after_body = split_frontmatter(text.replace("\r\n", "\n"))
        assert after_body == before_body
        assert after_fm.pop("not_yet_specified") == ["pricing model"]
        after_fm.pop("updated", None)
        before_fm.pop("updated", None)
        assert after_fm == before_fm

    def test_unparseable_frontmatter_is_refused_not_replaced(self, isolated_portfolio):
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Fog task")
        assert task is not None and task.file_path is not None
        broken = b"---\nid: [unclosed\n---\n# body\n"
        task.file_path.write_bytes(broken)
        with pytest.raises(ValueError):
            edit_fog(
                isolated_portfolio.config, isolated_portfolio.project_id, task.id,
                add="x",
            )
        assert task.file_path.read_bytes() == broken

    def test_cli_add_and_drop(self, isolated_portfolio):
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Fog task")
        runner = CliRunner()
        r1 = runner.invoke(
            main, ["tasks", "fog", task.id, "--project", isolated_portfolio.project_id, "--add", "pricing model"],
        )
        assert r1.exit_code == 0, r1.output
        assert json.loads(r1.output)["data"]["not_yet_specified"] == ["pricing model"]

        r2 = runner.invoke(
            main, ["tasks", "fog", task.id, "--project", isolated_portfolio.project_id, "--drop", "pricing model"],
        )
        assert r2.exit_code == 0, r2.output
        assert json.loads(r2.output)["data"]["not_yet_specified"] == []

    def test_cli_no_flags_is_usage_error(self, isolated_portfolio):
        task = add_task(isolated_portfolio.config, isolated_portfolio.project_id, "Fog task")
        runner = CliRunner()
        result = runner.invoke(main, ["tasks", "fog", task.id, "--project", isolated_portfolio.project_id])
        assert result.exit_code != 0
        assert "Provide --add and/or --drop" in result.output

    def test_cli_unknown_task_is_clean_error(self, isolated_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["tasks", "fog", "NOPE-999", "--project", isolated_portfolio.project_id, "--add", "x"],
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "task_not_found"


class TestShowWiring:
    def test_tasks_show_text_renders_map_sections(self, isolated_portfolio):
        root = _make_root_with_fog(isolated_portfolio, ["pricing model"])
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--format", "text", "tasks", "show", root.id, "--project", isolated_portfolio.project_id],
        )
        assert result.exit_code == 0, result.output
        assert "## Not yet specified" in result.output
        assert "pricing model" in result.output
