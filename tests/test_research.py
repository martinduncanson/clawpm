"""Unit tests for clawpm.research add/list/get/link paths (CLAWP-081).

research.py had only indirect coverage before this. These exercise the module
functions directly against an isolated portfolio.
"""

from __future__ import annotations

import pytest
import yaml

from clawpm.research import (
    add_research,
    get_research,
    get_research_dir,
    link_research_session,
    list_research,
)
from clawpm.models import ResearchStatus, ResearchType


class TestGetResearchDir:
    def test_returns_research_subdir_of_project(self, isolated_portfolio):
        d = get_research_dir(isolated_portfolio.config, "test")
        assert d is not None
        assert d.name == "research"
        assert d.parent == isolated_portfolio.project_dir / ".project"

    def test_unknown_project_returns_none(self, isolated_portfolio):
        assert get_research_dir(isolated_portfolio.config, "nope") is None


class TestAddResearch:
    def test_add_creates_file_and_returns_item(self, isolated_portfolio):
        item = add_research(
            isolated_portfolio.config,
            "test",
            title="Spike on caching",
            research_type=ResearchType.SPIKE,
            question="Does an LRU help?",
        )
        assert item is not None
        assert item.type == ResearchType.SPIKE
        assert item.status == ResearchStatus.OPEN
        assert item.file_path is not None and item.file_path.exists()
        assert "Does an LRU help?" in item.file_path.read_text(encoding="utf-8")

    def test_add_with_tags_persists_tags(self, isolated_portfolio):
        item = add_research(
            isolated_portfolio.config,
            "test",
            title="Tagged",
            research_type=ResearchType.INVESTIGATION,
            tags=["perf", "cache"],
        )
        assert item is not None
        assert set(item.tags) == {"perf", "cache"}

    def test_add_with_explicit_id(self, isolated_portfolio):
        item = add_research(
            isolated_portfolio.config,
            "test",
            title="Fixed id",
            research_type=ResearchType.DECISION,
            research_id="test-research-fixed",
        )
        assert item is not None
        assert item.id == "test-research-fixed"

    def test_add_unknown_project_returns_none(self, isolated_portfolio):
        assert (
            add_research(
                isolated_portfolio.config,
                "nope",
                title="x",
                research_type=ResearchType.SPIKE,
            )
            is None
        )

    def test_add_twice_same_title_yields_distinct_files(self, isolated_portfolio):
        a = add_research(
            isolated_portfolio.config, "test", "Dup", ResearchType.SPIKE
        )
        b = add_research(
            isolated_portfolio.config, "test", "Dup", ResearchType.SPIKE
        )
        assert a is not None and b is not None
        assert a.file_path != b.file_path


class TestListResearch:
    def test_empty_when_none(self, isolated_portfolio):
        assert list_research(isolated_portfolio.config, "test") == []

    def test_lists_added_items(self, isolated_portfolio):
        add_research(isolated_portfolio.config, "test", "One", ResearchType.SPIKE)
        add_research(
            isolated_portfolio.config, "test", "Two", ResearchType.INVESTIGATION
        )
        items = list_research(isolated_portfolio.config, "test")
        assert len(items) == 2

    def test_status_filter(self, isolated_portfolio):
        add_research(isolated_portfolio.config, "test", "Open item", ResearchType.SPIKE)
        assert (
            list_research(
                isolated_portfolio.config, "test", status_filter=ResearchStatus.COMPLETE
            )
            == []
        )
        assert (
            len(
                list_research(
                    isolated_portfolio.config,
                    "test",
                    status_filter=ResearchStatus.OPEN,
                )
            )
            == 1
        )

    def test_tags_filter_requires_all_tags(self, isolated_portfolio):
        add_research(
            isolated_portfolio.config,
            "test",
            "Multi",
            ResearchType.SPIKE,
            tags=["a", "b"],
        )
        add_research(
            isolated_portfolio.config,
            "test",
            "Single",
            ResearchType.SPIKE,
            tags=["a"],
        )
        both = list_research(isolated_portfolio.config, "test", tags_filter=["a", "b"])
        assert len(both) == 1
        assert both[0].title == "Multi"


class TestGetResearch:
    def test_get_by_id(self, isolated_portfolio):
        item = add_research(
            isolated_portfolio.config,
            "test",
            "Findable",
            ResearchType.SPIKE,
            research_id="test-research-findable",
        )
        assert item is not None
        got = get_research(
            isolated_portfolio.config, "test", "test-research-findable"
        )
        assert got is not None
        assert got.id == "test-research-findable"

    def test_get_missing_returns_none(self, isolated_portfolio):
        assert get_research(isolated_portfolio.config, "test", "no-such-id") is None


class TestLinkResearchSession:
    def test_link_sets_openclaw_and_in_progress(self, isolated_portfolio):
        item = add_research(
            isolated_portfolio.config,
            "test",
            "Linkable",
            ResearchType.SPIKE,
            research_id="test-research-linkable",
        )
        assert item is not None

        linked = link_research_session(
            isolated_portfolio.config,
            "test",
            "test-research-linkable",
            session_key="sess-123",
            run_id="run-9",
            spawned_by="claude-code",
        )
        assert linked is not None
        assert linked.status == ResearchStatus.IN_PROGRESS
        assert linked.openclaw is not None
        assert linked.openclaw["child_session_key"] == "sess-123"
        assert linked.openclaw["run_id"] == "run-9"
        assert linked.openclaw["spawned_by"] == "claude-code"

        # Frontmatter on disk reflects the link.
        text = linked.file_path.read_text(encoding="utf-8")
        fm = yaml.safe_load(text.split("---", 2)[1])
        assert fm["status"] == "in-progress"
        assert fm["openclaw"]["child_session_key"] == "sess-123"

    def test_link_missing_item_returns_none(self, isolated_portfolio):
        assert (
            link_research_session(
                isolated_portfolio.config, "test", "missing", session_key="s"
            )
            is None
        )

    def test_link_non_mapping_frontmatter_returns_none_not_typeerror(self, isolated_portfolio):
        """CLAWP-091: pins the (considered, then reverted) not_a_mapping
        guard in link_research_session as genuinely UNREACHABLE, not just
        untested — so a future attempt to "fix" it in isolation doesn't
        get surprised by the same DID-NOT-RAISE failure this one did.

        get_research() resolves items by comparing the FRONTMATTER's own
        `id` field to research_id (research filenames are date-prefixed,
        not the research_id itself). Once frontmatter is corrupted into a
        non-mapping, Research.from_file's lenient fallback can't recover an
        `id`, so get_research() can never match it — link_research_session's
        own `if not item: return None` fires before its internal
        split_frontmatter call ever sees the corrupted text. The result is
        a plain None (not found), same as the pre-existing absent/
        unterminated/unparseable policy — critically, NOT a raw TypeError.
        """
        item = add_research(
            isolated_portfolio.config,
            "test",
            "Corrupted",
            ResearchType.SPIKE,
            research_id="test-research-corrupted",
        )
        assert item is not None
        item.file_path.write_text(
            "---\n- not\n- a mapping\n---\n# Corrupted\n", encoding="utf-8"
        )

        result = link_research_session(
            isolated_portfolio.config,
            "test",
            "test-research-corrupted",
            session_key="sess-1",
        )
        assert result is None
        # And, whatever the outcome, the file must survive untouched.
        assert item.file_path.read_text(encoding="utf-8") == (
            "---\n- not\n- a mapping\n---\n# Corrupted\n"
        )


# ---------------------------------------------------------------------------
# CLAWP-095: research read-path hardening
# ---------------------------------------------------------------------------

import json  # noqa: E402
import logging  # noqa: E402

from click.testing import CliRunner  # noqa: E402

from clawpm.frontmatter import FrontmatterError  # noqa: E402
from clawpm.models import Research  # noqa: E402
from clawpm.research import scan_research  # noqa: E402


def _research_dir(iso):
    d = get_research_dir(iso.config, "test")
    d.mkdir(parents=True, exist_ok=True)
    return d


BAD_YAML = "---\nid: [unclosed\ntype: spike\n---\n# Bad\n"
BAD_ENUM = "---\nid: x-bad-enum\ntype: nonsense\n---\n# Bad enum\n"


class TestMalformedSurfaced:
    def test_from_file_raises_on_unparseable_yaml(self, isolated_portfolio):
        p = _research_dir(isolated_portfolio) / "bad.md"
        p.write_text(BAD_YAML, encoding="utf-8")
        with pytest.raises(FrontmatterError) as ei:
            Research.from_file(p)
        assert ei.value.reason == "unparseable"

    def test_from_file_still_lenient_without_frontmatter(self, isolated_portfolio):
        p = _research_dir(isolated_portfolio) / "plain.md"
        p.write_text("# Just notes\n", encoding="utf-8")
        item = Research.from_file(p)
        assert item.id == "plain" and item.title == "Just notes"

    def test_scan_reports_malformed_and_keeps_good(self, isolated_portfolio):
        add_research(isolated_portfolio.config, "test", "Good", ResearchType.SPIKE)
        d = _research_dir(isolated_portfolio)
        (d / "bad.md").write_text(BAD_YAML, encoding="utf-8")
        (d / "enum.md").write_text(BAD_ENUM, encoding="utf-8")
        scan = scan_research(isolated_portfolio.config, "test")
        assert len(scan.items) == 1
        assert {m["file"] for m in scan.malformed} == {"bad.md", "enum.md"}
        assert all(m["reason"] and m["message"] for m in scan.malformed)

    def test_list_research_stays_flat_list_but_logs(self, isolated_portfolio, caplog):
        add_research(isolated_portfolio.config, "test", "Good", ResearchType.SPIKE)
        (_research_dir(isolated_portfolio) / "bad.md").write_text(BAD_YAML, encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            items = list_research(isolated_portfolio.config, "test")
        assert isinstance(items, list) and len(items) == 1
        assert "bad.md" in caplog.text

    def test_get_research_logs_skipped_malformed(self, isolated_portfolio, caplog):
        (_research_dir(isolated_portfolio) / "bad.md").write_text(BAD_YAML, encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert get_research(isolated_portfolio.config, "test", "x") is None
        assert "bad.md" in caplog.text

    def test_malformed_ignores_status_filter(self, isolated_portfolio):
        (_research_dir(isolated_portfolio) / "bad.md").write_text(BAD_YAML, encoding="utf-8")
        scan = scan_research(
            isolated_portfolio.config, "test", status_filter=ResearchStatus.COMPLETE
        )
        assert len(scan.malformed) == 1

    def test_cli_json_always_flat_array_diagnostics_on_stderr(self, isolated_portfolio):
        from clawpm.cli import main

        add_research(isolated_portfolio.config, "test", "Good", ResearchType.SPIKE)
        runner = CliRunner()
        args = ["--format", "json", "research", "list", "-p", "test"]
        clean = runner.invoke(main, args)
        assert isinstance(json.loads(clean.stdout), list)
        assert "research file" not in clean.stderr

        (_research_dir(isolated_portfolio) / "bad.md").write_text(BAD_YAML, encoding="utf-8")
        res = runner.invoke(main, args)
        payload = json.loads(res.stdout)
        assert isinstance(payload, list) and len(payload) == 1
        assert "bad.md" in res.stderr
        assert "1 research file" in res.stderr

        res_text = runner.invoke(main, ["--format", "text", "research", "list", "-p", "test"])
        assert "bad.md" in res_text.output

    def test_cli_with_diagnostics_envelope_is_stable(self, isolated_portfolio):
        from clawpm.cli import main

        add_research(isolated_portfolio.config, "test", "Good", ResearchType.SPIKE)
        runner = CliRunner()
        args = ["--format", "json", "research", "list", "-p", "test", "--with-diagnostics"]
        clean = json.loads(runner.invoke(main, args).stdout)
        assert clean["malformed"] == [] and clean["malformed_count"] == 0
        assert len(clean["research"]) == 1

        (_research_dir(isolated_portfolio) / "bad.md").write_text(BAD_YAML, encoding="utf-8")
        payload = json.loads(runner.invoke(main, args).stdout)
        assert payload["malformed_count"] == 1
        assert payload["malformed"][0]["file"] == "bad.md"
        assert len(payload["research"]) == 1

    def test_mcp_research_list_has_malformed_fields(self, isolated_portfolio, monkeypatch):
        from clawpm import mcp_server

        monkeypatch.setattr(mcp_server, "_load_config", lambda: isolated_portfolio.config)
        monkeypatch.setattr(mcp_server, "_resolve_project", lambda p: ("test", None))
        (_research_dir(isolated_portfolio) / "bad.md").write_text(BAD_YAML, encoding="utf-8")
        out = mcp_server.research_list()
        assert out["count"] == 0 and out["malformed_count"] == 1
        assert out["malformed"][0]["file"] == "bad.md"


class TestUniqueFrontmatterId:
    def test_same_title_gets_distinct_ids(self, isolated_portfolio):
        a = add_research(isolated_portfolio.config, "test", "Dup", ResearchType.SPIKE)
        b = add_research(isolated_portfolio.config, "test", "Dup", ResearchType.SPIKE)
        c = add_research(isolated_portfolio.config, "test", "Dup", ResearchType.SPIKE)
        assert len({a.id, b.id, c.id}) == 3
        assert len(list_research(isolated_portfolio.config, "test")) == 3
        assert get_research(isolated_portfolio.config, "test", b.id).file_path == b.file_path

    def test_id_reserves_filename_stem_ids(self, isolated_portfolio):
        d = _research_dir(isolated_portfolio)
        (d / "test-research-dup.md").write_text("# Plain note\n", encoding="utf-8")
        (d / "test-research-dup-2.md").write_text("---\ntype: spike\n---\n# no id\n", encoding="utf-8")
        a = add_research(isolated_portfolio.config, "test", "Dup", ResearchType.SPIKE)
        assert a.id == "test-research-dup-3"

    def test_id_scan_logs_skipped_malformed(self, isolated_portfolio, caplog):
        (_research_dir(isolated_portfolio) / "bad.md").write_text(BAD_YAML, encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            add_research(isolated_portfolio.config, "test", "Dup", ResearchType.SPIKE)
        assert "bad.md" in caplog.text

    def test_concurrent_adds_get_distinct_ids(self, isolated_portfolio):
        import threading

        results: list = []
        errors: list = []
        barrier = threading.Barrier(6)

        def work():
            try:
                barrier.wait()
                results.append(
                    add_research(isolated_portfolio.config, "test", "Dup", ResearchType.SPIKE)
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len({r.id for r in results}) == 6
        assert len(list_research(isolated_portfolio.config, "test")) == 6


class TestBadTags:
    def test_tags_null_is_empty_and_filter_does_not_crash(self, isolated_portfolio):
        (_research_dir(isolated_portfolio) / "n.md").write_text(
            "---\nid: n\ntype: spike\ntags:\n---\n# N\n", encoding="utf-8"
        )
        scan = scan_research(isolated_portfolio.config, "test", tags_filter=["x"])
        assert scan.items == [] and scan.malformed == []
        assert scan_research(isolated_portfolio.config, "test").items[0].tags == []

    def test_tags_string_is_surfaced_as_malformed(self, isolated_portfolio):
        (_research_dir(isolated_portfolio) / "s.md").write_text(
            "---\nid: s\ntype: spike\ntags: abc\n---\n# S\n", encoding="utf-8"
        )
        scan = scan_research(isolated_portfolio.config, "test", tags_filter=["a"])
        assert scan.items == []
        assert [m["file"] for m in scan.malformed] == ["s.md"]

    def test_filtering_guarded_per_file(self, isolated_portfolio, monkeypatch):
        add_research(isolated_portfolio.config, "test", "Good", ResearchType.SPIKE, tags=["x"])
        orig = Research.from_file

        def weird(path):
            item = orig(path)
            item.tags = None  # simulate a bad value slipping past load
            return item

        monkeypatch.setattr(Research, "from_file", staticmethod(weird))
        scan = scan_research(isolated_portfolio.config, "test", tags_filter=["x"])
        assert len(scan.malformed) == 1
