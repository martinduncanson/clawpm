"""Unit tests for clawpm.context resolution helpers (CLAWP-081).

context.py had no dedicated tests before this. These cover the pure ID helpers,
the context-file get/set roundtrip (isolated from the real ``~/.clawpm-context``),
cwd-based project detection, and ``resolve_project`` precedence.
"""

from __future__ import annotations

import pytest

from clawpm import context


class TestGetProjectPrefix:
    @pytest.mark.parametrize(
        "project_id,expected",
        [
            ("clawpm", "CLAWP"),
            ("my-project", "MYPRO"),
            ("my_project", "MYPRO"),
            ("ab", "AB"),
            ("a-b-c", "ABC"),
        ],
    )
    def test_prefix(self, project_id, expected):
        assert context.get_project_prefix(project_id) == expected


class TestExpandTaskId:
    @pytest.mark.parametrize(
        "ref,expected",
        [
            ("22", "CLAWP-022"),
            ("022", "CLAWP-022"),
            ("CLAWP-022", "CLAWP-022"),
            ("clawp-022", "CLAWP-022"),
            ("4-001", "CLAWP-004-001"),
            ("CLAWP-004-001", "CLAWP-004-001"),
        ],
    )
    def test_expand(self, ref, expected):
        assert context.expand_task_id(ref, "clawpm") == expected

    def test_unrecognized_returned_as_is(self):
        assert context.expand_task_id("weird_ref", "clawpm") == "weird_ref"


class TestContextFile:
    @pytest.fixture(autouse=True)
    def _isolate_context_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(context, "CONTEXT_FILE", tmp_path / ".clawpm-context")

    def test_get_none_when_absent(self):
        assert context.get_context_project() is None

    def test_set_then_get_roundtrip(self):
        context.set_context_project("myproj")
        assert context.get_context_project() == "myproj"

    def test_set_none_clears(self):
        context.set_context_project("myproj")
        context.set_context_project(None)
        assert context.get_context_project() is None
        assert not context.CONTEXT_FILE.exists()

    def test_empty_file_reads_as_none(self):
        context.CONTEXT_FILE.write_text("   \n", encoding="utf-8")
        assert context.get_context_project() is None


class TestDetectProjectFromCwd:
    def test_detects_project_at_cwd(self, isolated_portfolio, monkeypatch):
        monkeypatch.chdir(isolated_portfolio.project_dir)
        proj = context.detect_project_from_cwd()
        assert proj is not None
        assert proj.id == "test"

    def test_none_outside_any_project(self, isolated_portfolio, tmp_path, monkeypatch):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.chdir(outside)
        assert context.detect_project_from_cwd() is None


class TestResolveProject:
    def test_explicit_wins(self, isolated_portfolio):
        assert context.resolve_project("given") == ("given", "explicit")

    def test_cwd_detection(self, isolated_portfolio, monkeypatch):
        monkeypatch.chdir(isolated_portfolio.project_dir)
        assert context.resolve_project() == ("test", "cwd")

    def test_context_fallback(self, isolated_portfolio, tmp_path, monkeypatch):
        # cwd has no project, so resolution falls through to the context file.
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.chdir(outside)
        monkeypatch.setattr(context, "CONTEXT_FILE", tmp_path / ".clawpm-context")
        context.set_context_project("ctxproj")
        assert context.resolve_project() == ("ctxproj", "context")

    def test_none_when_nothing_resolves(self, isolated_portfolio, tmp_path, monkeypatch):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.chdir(outside)
        monkeypatch.setattr(context, "CONTEXT_FILE", tmp_path / ".clawpm-context")
        assert context.resolve_project() == (None, "none")
