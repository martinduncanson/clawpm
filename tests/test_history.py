"""Tests for the history-import module (clawpm reflect history-import).

Covers the VT-clean restoration of the deleted sessions extractor:
- No hardcoded paths (source from --source / env var)
- Generic JSONL scanner (works on any agent runtime's log format)
- Bounded output (output_limit + max_files)
- Surfaces, doesn't swallow (file errors → empty list, not crash)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.history import (
    DEFAULT_OUTPUT_LIMIT,
    TASK_ID_RE,
    TaskMention,
    extract_task_mentions,
    find_log_files,
    import_history,
)


# ---------------------------------------------------------------------------
# Unit: TASK_ID_RE
# ---------------------------------------------------------------------------


class TestTaskIdRegex:
    @pytest.mark.parametrize("text,expected", [
        ("CLAWP-011", ["CLAWP-011"]),
        ("ALPHA-001 and BETA-042", ["ALPHA-001", "BETA-042"]),
        ("Working on CLAWP-011 + CLAWP-012", ["CLAWP-011", "CLAWP-012"]),
        ("MY_PROJ-99", ["MY_PROJ-99"]),
        ("PRJ-1", ["PRJ-1"]),
        # Codex PR#5 round-2 P1: multi-hyphen prefixes (produced when a
        # project ID's normalisation leaves embedded hyphens) must match
        # in full — not truncate to the last `<chunk>-<n>` segment.
        ("MY-PR-001", ["MY-PR-001"]),
        ("A-B-C-123", ["A-B-C-123"]),
        ("Working on MY-PR-001 and ALPHA-002", ["MY-PR-001", "ALPHA-002"]),
    ])
    def test_matches(self, text, expected):
        assert TASK_ID_RE.findall(text) == expected

    @pytest.mark.parametrize("text", [
        "lowercase-001",         # lowercase prefix
        "X-001",                 # prefix too short (need ≥2 chars)
        "CLAWP-",                # no number
        "ABCDEFGHIJK-1",         # prefix too long (>10)
        "CLAWP-123456",          # number too long (>5)
    ])
    def test_no_match(self, text):
        assert TASK_ID_RE.findall(text) == []


# ---------------------------------------------------------------------------
# Unit: find_log_files
# ---------------------------------------------------------------------------


class TestFindLogFiles:
    def test_empty_dir_returns_empty(self, tmp_path):
        assert find_log_files(tmp_path) == []

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        assert find_log_files(tmp_path / "nonexistent") == []

    def test_finds_jsonl_recursively(self, tmp_path):
        (tmp_path / "a.jsonl").touch()
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.jsonl").touch()
        (tmp_path / "sub" / "c.txt").touch()  # ignored
        result = find_log_files(tmp_path)
        names = [p.name for p in result]
        assert "a.jsonl" in names
        assert "b.jsonl" in names
        assert "c.txt" not in names

    def test_sorted_output(self, tmp_path):
        (tmp_path / "z.jsonl").touch()
        (tmp_path / "a.jsonl").touch()
        result = find_log_files(tmp_path)
        assert result[0].name == "a.jsonl"
        assert result[1].name == "z.jsonl"


# ---------------------------------------------------------------------------
# Unit: extract_task_mentions
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, *entries: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


class TestExtractTaskMentions:
    def test_single_mention(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_jsonl(p, {"timestamp": "2026-05-22T10:00:00Z", "msg": "Working on CLAWP-011"})
        mentions = extract_task_mentions(p)
        assert len(mentions) == 1
        assert mentions[0].task_id == "CLAWP-011"
        assert mentions[0].timestamp == "2026-05-22T10:00:00Z"
        assert mentions[0].line_no == 1

    def test_multiple_task_ids_same_line(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_jsonl(p, {"ts": "2026-05-22", "msg": "Closed CLAWP-011 and CLAWP-012"})
        mentions = extract_task_mentions(p)
        ids = {m.task_id for m in mentions}
        assert ids == {"CLAWP-011", "CLAWP-012"}

    def test_dedupe_same_id_in_same_line(self, tmp_path):
        # CLAWP-011 mentioned 3 times in one entry → 1 TaskMention, not 3
        p = tmp_path / "log.jsonl"
        _write_jsonl(p, {"msg": "CLAWP-011 ref CLAWP-011 see also CLAWP-011"})
        mentions = extract_task_mentions(p)
        assert len(mentions) == 1
        assert mentions[0].task_id == "CLAWP-011"

    def test_lines_without_task_ids_skipped(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_jsonl(
            p,
            {"msg": "Random log entry, no task ref"},
            {"msg": "CLAWP-011 here"},
            {"msg": "more chatter"},
        )
        mentions = extract_task_mentions(p)
        assert len(mentions) == 1

    def test_malformed_json_lines_skipped(self, tmp_path):
        p = tmp_path / "log.jsonl"
        p.write_text(
            "not valid json CLAWP-011\n"
            '{"msg": "CLAWP-012"}\n',
            encoding="utf-8",
        )
        mentions = extract_task_mentions(p)
        # Only the valid line should yield a mention
        assert len(mentions) == 1
        assert mentions[0].task_id == "CLAWP-012"

    def test_text_snippet_truncated(self, tmp_path):
        p = tmp_path / "log.jsonl"
        long_msg = "CLAWP-011 " + ("x" * 2000)
        _write_jsonl(p, {"msg": long_msg})
        mentions = extract_task_mentions(p, output_limit=100)
        assert len(mentions[0].text_snippet) == 100

    def test_unreadable_file_returns_empty(self, tmp_path):
        # Nonexistent file → empty list, no crash
        assert extract_task_mentions(tmp_path / "nope.jsonl") == []

    def test_timestamp_extraction_variants(self, tmp_path):
        p = tmp_path / "log.jsonl"
        _write_jsonl(
            p,
            {"timestamp": "2026-05-22T10:00:00Z", "msg": "CLAWP-001"},
            {"ts": "2026-05-22T11:00:00Z", "msg": "CLAWP-002"},
            {"occurred_at": "2026-05-22T12:00:00Z", "msg": "CLAWP-003"},
            {"created_at": "2026-05-22T13:00:00Z", "msg": "CLAWP-004"},
            {"message": {"timestamp": "2026-05-22T14:00:00Z"}, "msg": "CLAWP-005"},
            {"msg": "CLAWP-006"},  # no timestamp
        )
        mentions = extract_task_mentions(p)
        by_id = {m.task_id: m.timestamp for m in mentions}
        assert by_id["CLAWP-001"] == "2026-05-22T10:00:00Z"
        assert by_id["CLAWP-002"] == "2026-05-22T11:00:00Z"
        assert by_id["CLAWP-003"] == "2026-05-22T12:00:00Z"
        assert by_id["CLAWP-004"] == "2026-05-22T13:00:00Z"
        assert by_id["CLAWP-005"] == "2026-05-22T14:00:00Z"
        assert by_id["CLAWP-006"] == ""


# ---------------------------------------------------------------------------
# Unit: import_history
# ---------------------------------------------------------------------------


class TestImportHistory:
    def test_aggregate_report_shape(self, tmp_path):
        _write_jsonl(tmp_path / "a.jsonl",
            {"ts": "2026-05-22", "msg": "CLAWP-011 progress"},
            {"ts": "2026-05-22", "msg": "CLAWP-011 done"},
        )
        _write_jsonl(tmp_path / "b.jsonl",
            {"ts": "2026-05-22", "msg": "CLAWP-012"},
        )
        report = import_history(tmp_path)
        assert report["files_scanned"] == 2
        assert report["mentions_found"] == 3
        assert report["unique_task_ids"] == 2
        assert report["by_task"] == {"CLAWP-011": 2, "CLAWP-012": 1}
        assert report["files_truncated"] is False
        assert len(report["mentions"]) == 3

    def test_max_files_truncation_surfaces(self, tmp_path):
        for i in range(5):
            (tmp_path / f"f{i}.jsonl").touch()
        report = import_history(tmp_path, max_files=2)
        assert report["files_scanned"] == 2
        assert report["files_truncated"] is True

    def test_empty_source_dir(self, tmp_path):
        report = import_history(tmp_path)
        assert report["files_scanned"] == 0
        assert report["mentions_found"] == 0
        assert report["unique_task_ids"] == 0
        assert report["by_task"] == {}
        assert report["mentions"] == []

    def test_by_task_sorted(self, tmp_path):
        _write_jsonl(tmp_path / "log.jsonl",
            {"msg": "CHARLIE-001"},
            {"msg": "ALPHA-001"},
            {"msg": "BRAVO-001"},
        )
        report = import_history(tmp_path)
        # Dict ordering preserved from sorted() — Python 3.7+ guarantees insertion order
        assert list(report["by_task"].keys()) == ["ALPHA-001", "BRAVO-001", "CHARLIE-001"]


# ---------------------------------------------------------------------------
# CLI wiring: reflect history-import
# ---------------------------------------------------------------------------


class TestReflectHistoryImportCLI:
    def test_no_source_arg_returns_no_source_status(self, tmp_path, monkeypatch):
        # No --source, no CLAWPM_HISTORY_SOURCE env
        monkeypatch.delenv("CLAWPM_HISTORY_SOURCE", raising=False)
        runner = CliRunner()
        result = runner.invoke(main, ["reflect", "history-import"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "no_source"

    def test_nonexistent_source_dir(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(main, [
            "reflect", "history-import",
            "--source", str(tmp_path / "does-not-exist"),
        ])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "source_not_found"

    def test_source_via_env_var(self, tmp_path, monkeypatch):
        _write_jsonl(tmp_path / "log.jsonl", {"msg": "Working on CLAWP-011"})
        monkeypatch.setenv("CLAWPM_HISTORY_SOURCE", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(main, ["reflect", "history-import"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "scanned"
        assert data["unique_task_ids"] == 1
        assert "CLAWP-011" in data["by_task"]

    def test_source_via_flag(self, tmp_path):
        _write_jsonl(tmp_path / "log.jsonl", {"msg": "ALPHA-001 and BETA-042"})
        runner = CliRunner()
        result = runner.invoke(main, [
            "reflect", "history-import",
            "--source", str(tmp_path),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "scanned"
        assert data["by_task"]["ALPHA-001"] == 1
        assert data["by_task"]["BETA-042"] == 1

    def test_multi_hyphen_task_id_via_cli(self, tmp_path):
        # End-to-end coverage of TASK_ID_RE multi-hyphen support through the
        # CLI. Pairs the unit-level regex test with a transport-level
        # confidence that the wiring carries the fix through to report output.
        _write_jsonl(tmp_path / "log.jsonl",
            {"msg": "Working on MY-PR-001"},
            {"msg": "Followed up on MY-PR-001 and ALPHA-002"},
        )
        runner = CliRunner()
        result = runner.invoke(main, [
            "reflect", "history-import",
            "--source", str(tmp_path),
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "scanned"
        assert "MY-PR-001" in data["by_task"], data["by_task"]
        assert data["by_task"]["MY-PR-001"] == 2
        assert data["by_task"]["ALPHA-002"] == 1

    def test_no_mentions_returns_no_mentions_status(self, tmp_path):
        _write_jsonl(tmp_path / "log.jsonl", {"msg": "boring chatter no task refs"})
        runner = CliRunner()
        result = runner.invoke(main, [
            "reflect", "history-import",
            "--source", str(tmp_path),
        ])
        data = json.loads(result.output)
        assert data["status"] == "no_mentions"
        assert data["mentions_found"] == 0


# ---------------------------------------------------------------------------
# VT-clean discipline checks (regression guards)
# ---------------------------------------------------------------------------


class TestVTCleanDiscipline:
    """Regression guards against the patterns that got the original removed."""

    def test_no_hardcoded_agent_runtime_paths_in_module(self):
        # Per the design constraint comment in cli.py: NO hardcoded paths to
        # known agent-runtime session directories.
        module_text = Path(__file__).parent.parent.joinpath("src/clawpm/history.py").read_text(encoding="utf-8")
        forbidden = [".openclaw", ".claude/projects", "agents/main/sessions"]
        for needle in forbidden:
            assert needle not in module_text, f"history.py contains forbidden hardcoded path: {needle!r}"

    def test_module_not_statically_imported_by_cli(self):
        # The lazy-import discipline: cli.py must not have `from clawpm.history`
        # or `import clawpm.history` at module top level. Only inside the command
        # function body.
        cli_text = Path(__file__).parent.parent.joinpath("src/clawpm/cli.py").read_text(encoding="utf-8")
        # Find the first import block (up to first @click decorator or function def)
        head = cli_text.split("@main")[0] if "@main" in cli_text else cli_text[:5000]
        assert "from clawpm.history" not in head
        assert "import clawpm.history" not in head
