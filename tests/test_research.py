"""Unit tests for clawpm.research add/list/get/link paths (CLAWP-081).

research.py had only indirect coverage before this. These exercise the module
functions directly against an isolated portfolio.
"""

from __future__ import annotations

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
