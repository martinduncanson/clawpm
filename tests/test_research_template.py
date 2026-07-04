"""Tests for research entry template + single-shot capture (CLAWP-087)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import (
    PLACEHOLDER_STALE_DAYS,
    Research,
    ResearchStatus,
    ResearchType,
    has_placeholder_sections,
    is_stale_placeholder,
)
from clawpm.research import add_research


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_research_test_")
    portfolio_root = Path(temp_dir)
    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n'
    )
    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    project_dir = projects_dir / "test-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
    )
    (project_meta / "tasks").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    config = load_portfolio_config(portfolio_root)
    yield {"root": portfolio_root, "config": config}
    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def test_has_placeholder_detects_to_be_filled_marker():
    assert has_placeholder_sections("## Summary\n\n(To be filled in as research progresses)\n")


def test_has_placeholder_detects_question_stub():
    assert has_placeholder_sections("## Question\n\n(Describe the research question)\n")


def test_has_placeholder_detects_ellipsis_stub():
    assert has_placeholder_sections("## Findings\n\n...\n\n## Conclusion\n\n...\n")


def test_has_placeholder_detects_ellipsis_without_blank_line():
    # A bare "## Findings\n...\n" stub (no extra blank line) must still count.
    assert has_placeholder_sections("## Findings\n...\n")


def test_has_placeholder_ignores_prose_ellipsis():
    # A genuine ellipsis mid-sentence must not be read as a stub.
    assert not has_placeholder_sections("## Summary\n\nWe tried A... then B, and shipped.\n")


def test_has_placeholder_ignores_marker_quoted_in_prose():
    # A filled entry that merely quotes the stub phrase mid-body isn't flagged.
    body = "## Summary\n\nThe old note said (To be filled in) but it's now resolved.\n"
    assert not has_placeholder_sections(body)


def test_has_placeholder_false_on_filled_body():
    body = "## Question\n\nDoes X help?\n\n## Summary\n\nYes, adopt X for reason Y.\n"
    assert not has_placeholder_sections(body)


# ---------------------------------------------------------------------------
# is_stale_placeholder
# ---------------------------------------------------------------------------


def _research(content: str, *, created: str | None, status=ResearchStatus.OPEN) -> Research:
    return Research(
        id="r1",
        title="R1",
        type=ResearchType.INVESTIGATION,
        status=status,
        created=created,
        content=content,
    )


def test_stale_placeholder_flags_old_open_stub():
    old = (datetime.now(timezone.utc) - timedelta(days=PLACEHOLDER_STALE_DAYS + 5)).date().isoformat()
    item = _research("## Summary\n\n(To be filled in)\n", created=old)
    assert is_stale_placeholder(item)
    assert item.is_stale_placeholder()


def test_stale_placeholder_not_flagged_when_recent():
    recent = datetime.now(timezone.utc).date().isoformat()
    item = _research("## Summary\n\n(To be filled in)\n", created=recent)
    assert not is_stale_placeholder(item)


def test_stale_placeholder_not_flagged_when_filled():
    old = (datetime.now(timezone.utc) - timedelta(days=100)).date().isoformat()
    item = _research("## Summary\n\nReal verdict here.\n", created=old)
    assert not is_stale_placeholder(item)


def test_stale_placeholder_not_flagged_when_complete():
    old = (datetime.now(timezone.utc) - timedelta(days=100)).date().isoformat()
    item = _research("## Summary\n\n(To be filled in)\n", created=old, status=ResearchStatus.COMPLETE)
    assert not is_stale_placeholder(item)


def test_stale_placeholder_flags_missing_created_date():
    # No usable date + still stubbed -> surface it rather than silently ignore.
    item = _research("## Summary\n\n(To be filled in)\n", created=None)
    assert is_stale_placeholder(item)


def test_stale_placeholder_handles_tz_aware_created():
    # A full ISO timestamp with an offset must be honoured, not clobbered.
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    item = _research("## Summary\n\n(To be filled in)\n", created=old)
    assert is_stale_placeholder(item)
    recent = datetime.now(timezone.utc).isoformat()
    item2 = _research("## Summary\n\n(To be filled in)\n", created=recent)
    assert not is_stale_placeholder(item2)


# ---------------------------------------------------------------------------
# add_research template modes
# ---------------------------------------------------------------------------


def test_add_research_single_shot_writes_sections(temp_portfolio):
    config = temp_portfolio["config"]
    item = add_research(
        config,
        "test",
        "MCP eval",
        ResearchType.INVESTIGATION,
        question="Does X help?",
        summary="Yes, adopt X.",
        findings=["X is fast", "X is MIT"],
        conclusion="Ship it.",
    )
    assert item is not None
    text = item.file_path.read_text(encoding="utf-8")
    assert "## Summary\n\nYes, adopt X." in text
    assert "- X is fast" in text
    assert "- X is MIT" in text
    assert "## Conclusion\n\nShip it." in text
    assert not has_placeholder_sections(text)
    assert not item.is_stale_placeholder()


def test_add_research_single_shot_omits_empty_sections(temp_portfolio):
    config = temp_portfolio["config"]
    item = add_research(
        config,
        "test",
        "Verdict only",
        ResearchType.DECISION,
        summary="Just a verdict.",
    )
    text = item.file_path.read_text(encoding="utf-8")
    assert "## Summary" in text
    # No empty Findings/Conclusion stubs when nothing was supplied for them.
    assert "## Findings" not in text
    assert "## Conclusion" not in text
    assert "..." not in text
    assert not has_placeholder_sections(text)


def test_add_research_open_keeps_progressive_template(temp_portfolio):
    config = temp_portfolio["config"]
    item = add_research(
        config,
        "test",
        "Open investigation",
        ResearchType.INVESTIGATION,
        question="Open question?",
        open_ended=True,
    )
    text = item.file_path.read_text(encoding="utf-8")
    assert "## Summary" in text
    assert "## Findings" in text
    assert "## Conclusion" in text
    # Progressive template is legitimately stubbed at creation.
    assert has_placeholder_sections(text)


def test_to_dict_includes_has_placeholder(temp_portfolio):
    config = temp_portfolio["config"]
    filled = add_research(config, "test", "A", ResearchType.SPIKE, summary="done")
    stub = add_research(config, "test", "B", ResearchType.SPIKE, open_ended=True)
    assert filled.to_dict()["has_placeholder"] is False
    assert stub.to_dict()["has_placeholder"] is True


# ---------------------------------------------------------------------------
# CLI: research add
# ---------------------------------------------------------------------------


def test_cli_add_requires_summary_or_open(temp_portfolio):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--format", "json", "research", "add", "-p", "test",
         "-t", "investigation", "--title", "No verdict"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["error"] == "missing_verdict"


def test_cli_add_findings_only_requires_verdict(temp_portfolio):
    # --finding / --conclusion alone don't substitute for the Summary verdict.
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--format", "json", "research", "add", "-p", "test",
         "-t", "investigation", "--title", "No summary", "--finding", "x"],
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "missing_verdict"


def test_cli_add_open_conflicts_with_capture(temp_portfolio):
    # --open cannot carry a verdict — the content would be silently dropped.
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--format", "json", "research", "add", "-p", "test",
         "-t", "investigation", "--title", "Conflict", "--open", "--summary", "verdict"],
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "open_conflict"


def test_cli_add_single_shot_succeeds(temp_portfolio):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--format", "json", "research", "add", "-p", "test",
         "-t", "decision", "--title", "Adopt X",
         "--summary", "Yes adopt X", "--finding", "fast", "--finding", "cheap"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["has_placeholder"] is False
    path = Path(payload["data"]["file_path"])
    text = path.read_text(encoding="utf-8")
    assert "- fast" in text and "- cheap" in text
    assert not has_placeholder_sections(text)


def test_cli_add_verdict_alias(temp_portfolio):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--format", "json", "research", "add", "-p", "test",
         "-t", "decision", "--title", "Alias", "--verdict", "adopt"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["has_placeholder"] is False


def test_cli_add_open_succeeds_with_stub(temp_portfolio):
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--format", "json", "research", "add", "-p", "test",
         "-t", "investigation", "--title", "Open one", "--open"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["has_placeholder"] is True


# ---------------------------------------------------------------------------
# CLI: research list surfaces the staleness signal
# ---------------------------------------------------------------------------


def test_cli_list_reports_stale_placeholder(temp_portfolio):
    config = temp_portfolio["config"]
    # An open entry backdated past the threshold, still stubbed.
    item = add_research(config, "test", "Old open", ResearchType.INVESTIGATION, open_ended=True)
    text = item.file_path.read_text(encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(days=PLACEHOLDER_STALE_DAYS + 10)).date().isoformat()
    item.file_path.write_text(text.replace(f"created: '{item.created}'", f"created: '{old}'"), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["--format", "json", "research", "list", "-p", "test"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["stale_placeholder"] is True
    assert rows[0]["has_placeholder"] is True


def test_cli_list_single_shot_not_stale(temp_portfolio):
    config = temp_portfolio["config"]
    add_research(config, "test", "Fresh verdict", ResearchType.DECISION, summary="adopt")
    runner = CliRunner()
    result = runner.invoke(main, ["--format", "json", "research", "list", "-p", "test"])
    rows = json.loads(result.output)
    assert rows[0]["stale_placeholder"] is False


# ---------------------------------------------------------------------------
# Retrofit guard: shipped .project/research entries carry no rot
# ---------------------------------------------------------------------------


def test_shipped_research_entries_have_no_placeholder_rot():
    research_dir = Path(__file__).resolve().parent.parent / ".project" / "research"
    files = sorted(research_dir.glob("*.md"))
    assert files, "expected shipped research entries to guard"
    offenders = [f.name for f in files if has_placeholder_sections(f.read_text(encoding="utf-8"))]
    assert not offenders, f"placeholder rot in: {offenders}"
