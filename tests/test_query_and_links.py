"""Tests for CLAWP-082 — task query/filtering + wiki-link backlinks.

Covers:
  1. extract_wiki_links — the [[id]] parser (alias, dedupe, blanks).
  2. Query filters (filters.by_text/by_priority/by_complexity/by_parent/
     by_linked) + composition through apply_filters + --limit slicing.
  3. Link index: outbound links, backlinks unifying wiki + typed edges,
     referencing_ids, self-link drop.
  4. Dangling-link detection (find_dangling_links + doctor wiring).
  5. CLI end-to-end: tasks list filters, tasks show / context linked_from.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.filters import (
    apply_filters,
    by_complexity,
    by_linked,
    by_parent,
    by_priority,
    by_text,
)
from clawpm.links import build_link_index, find_dangling_links, extract_wiki_links as _elw
from clawpm.models import Task, TaskComplexity, TaskState, extract_wiki_links
from clawpm.tasks import add_subtask, add_task, get_task


def _mktask(**kw) -> Task:
    return Task(
        id=kw.pop("id", "T-1"),
        title=kw.pop("title", "t"),
        state=kw.pop("state", TaskState.OPEN),
        **kw,
    )


# ---------------------------------------------------------------------------
# 1. extract_wiki_links
# ---------------------------------------------------------------------------

class TestExtractWikiLinks:
    def test_basic(self):
        assert extract_wiki_links("see [[CLAWP-042]] now") == ["CLAWP-042"]

    def test_alias_keeps_target(self):
        assert extract_wiki_links("[[CLAWP-042|the auth task]]") == ["CLAWP-042"]

    def test_dedupes_preserving_order(self):
        text = "[[B]] then [[A]] then [[B]] again"
        assert extract_wiki_links(text) == ["B", "A"]

    def test_multiple(self):
        assert extract_wiki_links("[[A]] [[B]] [[C]]") == ["A", "B", "C"]

    def test_strips_whitespace(self):
        assert extract_wiki_links("[[  CLAWP-042  ]]") == ["CLAWP-042"]

    def test_blank_target_dropped(self):
        assert extract_wiki_links("[[]] and [[  ]] and [[X]]") == ["X"]

    def test_none_and_empty(self):
        assert extract_wiki_links(None) == []
        assert extract_wiki_links("") == []

    def test_no_links(self):
        assert extract_wiki_links("plain text, no links") == []

    def test_links_module_reexports_same_parser(self):
        # links.extract_wiki_links is the models one (no duplicate parser).
        assert _elw is extract_wiki_links


# ---------------------------------------------------------------------------
# 2. Query filters
# ---------------------------------------------------------------------------

class TestByText:
    def _tasks(self):
        return [
            _mktask(id="A", title="Migrate auth to JWT", content="# Migrate auth to JWT\n\nrefresh tokens"),
            _mktask(id="B", title="Fix cookie edge case", content="# Fix cookie edge case\n\nmobile webview"),
            _mktask(id="C", title="Refactor logging", content="# Refactor logging\n\nstructured JSON"),
        ]

    def test_substring_over_title(self):
        got = apply_filters(self._tasks(), [by_text("auth")])
        assert {t.id for t in got} == {"A"}

    def test_substring_over_body(self):
        got = apply_filters(self._tasks(), [by_text("webview")])
        assert {t.id for t in got} == {"B"}

    def test_case_insensitive(self):
        got = apply_filters(self._tasks(), [by_text("JwT")])
        assert {t.id for t in got} == {"A"}

    def test_regex(self):
        got = apply_filters(self._tasks(), [by_text(r"json|jwt", use_regex=True)])
        assert {t.id for t in got} == {"A", "C"}

    def test_blank_matches_nothing(self):
        assert apply_filters(self._tasks(), [by_text("   ")]) == []

    def test_bad_regex_matches_nothing(self):
        assert apply_filters(self._tasks(), [by_text("[unterminated", use_regex=True)]) == []


class TestByPriority:
    def _tasks(self):
        return [_mktask(id=str(p), priority=p) for p in (1, 3, 5, 7, 9)]

    def test_exact(self):
        got = apply_filters(self._tasks(), [by_priority("5")])
        assert {t.id for t in got} == {"5"}

    def test_lte(self):
        got = apply_filters(self._tasks(), [by_priority("<=3")])
        assert {t.id for t in got} == {"1", "3"}

    def test_gt(self):
        got = apply_filters(self._tasks(), [by_priority(">7")])
        assert {t.id for t in got} == {"9"}

    def test_gte(self):
        got = apply_filters(self._tasks(), [by_priority(">=7")])
        assert {t.id for t in got} == {"7", "9"}

    def test_int_input(self):
        got = apply_filters(self._tasks(), [by_priority(3)])
        assert {t.id for t in got} == {"3"}

    def test_bad_spec_matches_nothing(self):
        assert apply_filters(self._tasks(), [by_priority("high")]) == []


class TestByComplexity:
    def _tasks(self):
        return [
            _mktask(id="s", complexity=TaskComplexity.S),
            _mktask(id="m", complexity=TaskComplexity.M),
            _mktask(id="l", complexity=TaskComplexity.L),
            _mktask(id="none", complexity=None),
        ]

    def test_single(self):
        got = apply_filters(self._tasks(), [by_complexity(["l"])])
        assert {t.id for t in got} == {"l"}

    def test_or_multiple(self):
        got = apply_filters(self._tasks(), [by_complexity(["l", "xl", "s"])])
        assert {t.id for t in got} == {"s", "l"}

    def test_case_insensitive(self):
        got = apply_filters(self._tasks(), [by_complexity(["L"])])
        assert {t.id for t in got} == {"l"}

    def test_none_complexity_never_matches(self):
        got = apply_filters(self._tasks(), [by_complexity(["s", "m", "l", "xl"])])
        assert "none" not in {t.id for t in got}

    def test_empty_matches_nothing(self):
        assert apply_filters(self._tasks(), [by_complexity([])]) == []


class TestByParent:
    def _tasks(self):
        return [
            _mktask(id="P-1", parent=None),
            _mktask(id="P-1-1", parent="P-1"),
            _mktask(id="P-1-2", parent="P-1"),
            _mktask(id="Q-1-1", parent="Q-1"),
        ]

    def test_direct_children(self):
        got = apply_filters(self._tasks(), [by_parent("P-1")])
        assert {t.id for t in got} == {"P-1-1", "P-1-2"}

    def test_blank_matches_nothing(self):
        assert apply_filters(self._tasks(), [by_parent("")]) == []


class TestByLinked:
    def test_matches_referencing_set(self):
        tasks = [_mktask(id="A"), _mktask(id="B"), _mktask(id="C")]
        got = apply_filters(tasks, [by_linked({"A", "C"})])
        assert {t.id for t in got} == {"A", "C"}

    def test_empty_matches_nothing(self):
        tasks = [_mktask(id="A")]
        assert apply_filters(tasks, [by_linked(set())]) == []


class TestComposition:
    def test_filters_and_together(self):
        tasks = [
            _mktask(id="A", priority=2, complexity=TaskComplexity.L, title="alpha"),
            _mktask(id="B", priority=2, complexity=TaskComplexity.S, title="alpha"),
            _mktask(id="C", priority=8, complexity=TaskComplexity.L, title="alpha"),
        ]
        got = apply_filters(tasks, [by_priority("<=3"), by_complexity(["l"]), by_text("alpha")])
        assert {t.id for t in got} == {"A"}


# ---------------------------------------------------------------------------
# 3. Link index (integration)
# ---------------------------------------------------------------------------

class TestLinkIndex:
    def test_outbound_wiki_links(self, isolated_portfolio):
        ip = isolated_portfolio
        target = add_task(ip.config, ip.project_id, "Target")
        src = add_task(
            ip.config, ip.project_id, "Source",
            description=f"depends conceptually on [[{target.id}]]",
        )
        index = build_link_index(ip.config, ip.project_id)
        assert index.links_of(src.id) == [target.id]

    def test_wiki_backlink(self, isolated_portfolio):
        ip = isolated_portfolio
        target = add_task(ip.config, ip.project_id, "Target")
        src = add_task(
            ip.config, ip.project_id, "Source",
            description=f"see [[{target.id}]]",
        )
        index = build_link_index(ip.config, ip.project_id)
        backlinks = index.linked_from(target.id)
        assert {"id": src.id, "via": "wiki"} in backlinks

    def test_typed_edge_surfaces_in_backlinks(self, isolated_portfolio):
        ip = isolated_portfolio
        dep = add_task(ip.config, ip.project_id, "Dependency")
        consumer = add_task(
            ip.config, ip.project_id, "Consumer", depends=[dep.id],
        )
        index = build_link_index(ip.config, ip.project_id)
        backlinks = index.linked_from(dep.id)
        assert {"id": consumer.id, "via": "depends"} in backlinks

    def test_backlinks_unify_wiki_and_typed(self, isolated_portfolio):
        ip = isolated_portfolio
        hub = add_task(ip.config, ip.project_id, "Hub")
        dep = add_task(ip.config, ip.project_id, "Dep", depends=[hub.id])
        wiki = add_task(ip.config, ip.project_id, "Wiki", description=f"[[{hub.id}]]")
        index = build_link_index(ip.config, ip.project_id)
        refs = index.referencing_ids(hub.id)
        assert dep.id in refs and wiki.id in refs

    def test_subtask_parent_backlink(self, isolated_portfolio):
        ip = isolated_portfolio
        parent = add_task(ip.config, ip.project_id, "Parent")
        child = add_subtask(ip.config, ip.project_id, parent.id, "Child")
        index = build_link_index(ip.config, ip.project_id)
        vias = {lf["via"] for lf in index.linked_from(parent.id) if lf["id"] == child.id}
        assert "parent" in vias

    def test_self_link_dropped(self, isolated_portfolio):
        ip = isolated_portfolio
        t = add_task(ip.config, ip.project_id, "Selfie")
        # Rewrite body to reference itself.
        text = t.file_path.read_text(encoding="utf-8")
        t.file_path.write_text(text + f"\n\nSee [[{t.id}]].\n", encoding="utf-8")
        index = build_link_index(ip.config, ip.project_id)
        assert index.referencing_ids(t.id) == set()


# ---------------------------------------------------------------------------
# 4. Dangling links
# ---------------------------------------------------------------------------

class TestDanglingLinks:
    def test_dangling_detected(self, isolated_portfolio):
        ip = isolated_portfolio
        add_task(ip.config, ip.project_id, "Broken", description="[[CLAWP-DOES-NOT-EXIST]]")
        findings = find_dangling_links(ip.config, ip.project_id)
        assert any(f["target"] == "CLAWP-DOES-NOT-EXIST" for f in findings)

    def test_valid_link_not_flagged(self, isolated_portfolio):
        ip = isolated_portfolio
        target = add_task(ip.config, ip.project_id, "Real")
        add_task(ip.config, ip.project_id, "Ref", description=f"[[{target.id}]]")
        findings = find_dangling_links(ip.config, ip.project_id)
        assert findings == []

    def test_doctor_reports_dangling(self, isolated_portfolio):
        ip = isolated_portfolio
        add_task(ip.config, ip.project_id, "Broken", description="[[NOPE-001]]")
        runner = CliRunner()
        r = runner.invoke(main, [
            "doctor", "--project", ip.project_id,
        ], env={"CLAWPM_PORTFOLIO": ip.root.as_posix()})
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert any(d["target"] == "NOPE-001" for d in payload["dangling_links"])

    def test_doctor_strict_fails_on_dangling(self, isolated_portfolio):
        ip = isolated_portfolio
        add_task(ip.config, ip.project_id, "Broken", description="[[NOPE-002]]")
        runner = CliRunner()
        r = runner.invoke(main, [
            "doctor", "--project", ip.project_id, "--strict",
        ], env={"CLAWPM_PORTFOLIO": ip.root.as_posix()})
        assert r.exit_code == 1


# ---------------------------------------------------------------------------
# 5. CLI end-to-end
# ---------------------------------------------------------------------------

class TestCli:
    def _env(self, ip):
        return {"CLAWPM_PORTFOLIO": ip.root.as_posix()}

    def test_list_text_filter(self, isolated_portfolio):
        ip = isolated_portfolio
        add_task(ip.config, ip.project_id, "Migrate auth to JWT")
        add_task(ip.config, ip.project_id, "Refactor logging")
        runner = CliRunner()
        r = runner.invoke(main, [
            "tasks", "list", "--project", ip.project_id, "--text", "auth",
        ], env=self._env(ip))
        assert r.exit_code == 0, r.output
        titles = {t["title"] for t in json.loads(r.output)}
        assert titles == {"Migrate auth to JWT"}

    def test_list_priority_filter(self, isolated_portfolio):
        ip = isolated_portfolio
        add_task(ip.config, ip.project_id, "Urgent", priority=1)
        add_task(ip.config, ip.project_id, "Later", priority=9)
        runner = CliRunner()
        r = runner.invoke(main, [
            "tasks", "list", "--project", ip.project_id, "--priority", "<=3",
        ], env=self._env(ip))
        assert r.exit_code == 0, r.output
        titles = {t["title"] for t in json.loads(r.output)}
        assert titles == {"Urgent"}

    def test_list_complexity_filter(self, isolated_portfolio):
        ip = isolated_portfolio
        add_task(ip.config, ip.project_id, "Big", complexity=TaskComplexity.XL)
        add_task(ip.config, ip.project_id, "Small", complexity=TaskComplexity.S)
        runner = CliRunner()
        r = runner.invoke(main, [
            "tasks", "list", "--project", ip.project_id, "--complexity", "xl",
        ], env=self._env(ip))
        assert r.exit_code == 0, r.output
        titles = {t["title"] for t in json.loads(r.output)}
        assert titles == {"Big"}

    def test_list_parent_filter(self, isolated_portfolio):
        ip = isolated_portfolio
        parent = add_task(ip.config, ip.project_id, "Parent")
        child = add_subtask(ip.config, ip.project_id, parent.id, "Child")
        runner = CliRunner()
        r = runner.invoke(main, [
            "tasks", "list", "--project", ip.project_id, "-s", "all",
            "--parent", parent.id,
        ], env=self._env(ip))
        assert r.exit_code == 0, r.output
        ids = {t["id"] for t in json.loads(r.output)}
        assert ids == {child.id}

    def test_list_limit(self, isolated_portfolio):
        ip = isolated_portfolio
        for i in range(5):
            add_task(ip.config, ip.project_id, f"T{i}")
        runner = CliRunner()
        r = runner.invoke(main, [
            "tasks", "list", "--project", ip.project_id, "--limit", "2",
        ], env=self._env(ip))
        assert r.exit_code == 0, r.output
        assert len(json.loads(r.output)) == 2

    def test_list_linked_filter(self, isolated_portfolio):
        ip = isolated_portfolio
        target = add_task(ip.config, ip.project_id, "Target")
        ref = add_task(ip.config, ip.project_id, "Ref", description=f"[[{target.id}]]")
        add_task(ip.config, ip.project_id, "Unrelated")
        runner = CliRunner()
        r = runner.invoke(main, [
            "tasks", "list", "--project", ip.project_id, "--linked", target.id,
        ], env=self._env(ip))
        assert r.exit_code == 0, r.output
        ids = {t["id"] for t in json.loads(r.output)}
        assert ids == {ref.id}

    def test_filters_compose_cli(self, isolated_portfolio):
        ip = isolated_portfolio
        add_task(ip.config, ip.project_id, "alpha match", priority=2, complexity=TaskComplexity.L)
        add_task(ip.config, ip.project_id, "alpha nomatch", priority=8, complexity=TaskComplexity.L)
        runner = CliRunner()
        r = runner.invoke(main, [
            "tasks", "list", "--project", ip.project_id,
            "--text", "alpha", "--priority", "<=3", "--complexity", "l",
        ], env=self._env(ip))
        assert r.exit_code == 0, r.output
        titles = {t["title"] for t in json.loads(r.output)}
        assert titles == {"alpha match"}

    def test_show_returns_links_and_linked_from(self, isolated_portfolio):
        ip = isolated_portfolio
        target = add_task(ip.config, ip.project_id, "Target")
        ref = add_task(ip.config, ip.project_id, "Ref", description=f"points at [[{target.id}]]")
        runner = CliRunner()
        # Source task exposes its outbound wiki-link.
        r = runner.invoke(main, [
            "tasks", "show", ref.id, "--project", ip.project_id,
        ], env=self._env(ip))
        assert r.exit_code == 0, r.output
        src_dict = json.loads(r.output)
        assert target.id in src_dict["links"]
        # Target task exposes the backlink.
        r2 = runner.invoke(main, [
            "tasks", "show", target.id, "--project", ip.project_id,
        ], env=self._env(ip))
        assert r2.exit_code == 0, r2.output
        tgt_dict = json.loads(r2.output)
        assert {"id": ref.id, "via": "wiki"} in tgt_dict["linked_from"]

    def test_context_includes_linked_from(self, isolated_portfolio):
        ip = isolated_portfolio
        from clawpm.tasks import change_task_state
        target = add_task(ip.config, ip.project_id, "Target")
        change_task_state(ip.config, ip.project_id, target.id, TaskState.PROGRESS)
        add_task(ip.config, ip.project_id, "Ref", description=f"[[{target.id}]]")
        runner = CliRunner()
        r = runner.invoke(main, [
            "context", "--project", ip.project_id,
        ], env=self._env(ip))
        assert r.exit_code == 0, r.output
        ctx = json.loads(r.output)
        in_prog = {t["id"]: t for t in ctx["in_progress"]}
        assert target.id in in_prog
        assert "linked_from" in in_prog[target.id]
