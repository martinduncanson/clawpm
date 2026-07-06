from __future__ import annotations

import json
import sys

import click

from clawpm.output import OutputFormat, output_error, output_json
from clawpm.discovery import get_project
from clawpm.cli.base import main, get_format, require_portfolio, require_project

# ============================================================================
# Issues commands
# ============================================================================

@main.group("issues")
def issues_group() -> None:
    """Log and track issues found during work."""
    pass


ISSUE_TYPES = ["bug", "ux", "docs", "feature", "observation"]


@issues_group.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--type", "-t", "issue_type", type=click.Choice(ISSUE_TYPES), default="bug", help="Issue type")
@click.option("--severity", "-s", type=click.Choice(["high", "medium", "low"]), default="medium", help="Severity")
@click.option("--command", "-c", "cmd", help="Command that triggered the issue")
@click.option("--expected", "-e", help="What was expected")
@click.option("--actual", "-a", help="What actually happened")
@click.option("--context", help="Additional context")
@click.option("--tag", "tags", multiple=True, help="Free-form tag (repeatable, e.g. --tag depth-warning --tag ergonomic)")
@click.option("--summary", help="One-line summary (alternative to --expected/--actual for observation-type entries)")
@click.pass_context
def issues_add(
    ctx: click.Context,
    project_id: str | None,
    issue_type: str,
    severity: str,
    cmd: str | None,
    expected: str | None,
    actual: str | None,
    context: str | None,
    tags: tuple[str, ...],
    summary: str | None,
) -> None:
    """Log an issue for a project."""
    import json
    from datetime import datetime, timezone

    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)
    proj = get_project(config, project_id)

    if not proj:
        output_error("project_not_found", f"Project '{project_id}' not found", fmt=fmt)
        sys.exit(1)

    # Create .agent directory if needed
    agent_dir = proj.project_dir / ".agent"
    agent_dir.mkdir(exist_ok=True)
    issues_file = agent_dir / "issues.jsonl"

    entry = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "type": issue_type,
        "severity": severity,
        "command": cmd,
        "expected": expected,
        "actual": actual,
        "context": context,
        "summary": summary,
        "tags": list(tags) if tags else [],
        "fixed": False,
    }

    # Remove None values but always keep "tags" (even if empty list) and "fixed"
    entry = {k: v for k, v in entry.items() if v is not None}

    # CLAWP-032: cross-platform locked append. The previous non-atomic append
    # was the originating motivation for the concurrency audit — two parallel
    # `clawpm issues add` invocations on Windows could interleave JSON bytes
    # and silently corrupt `.agent/issues.jsonl`.
    from clawpm.concurrency import append_jsonl_line
    append_jsonl_line(issues_file, json.dumps(entry))

    if fmt == OutputFormat.JSON:
        output_json({"status": "logged", "file": str(issues_file), "entry": entry})
    else:
        click.echo(f"Logged {issue_type} issue ({severity}) to {issues_file}")


@issues_group.command("list")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--open", "open_only", is_flag=True, help="Show only unfixed issues")
@click.option("--type", "-t", "issue_type", type=click.Choice(ISSUE_TYPES), help="Filter by type")
@click.option("--tag", "tag_filter", multiple=True, help="Filter by tag (repeatable; entries matching ANY supplied tag are shown)")
@click.pass_context
def issues_list(
    ctx: click.Context,
    project_id: str | None,
    open_only: bool,
    issue_type: str | None,
    tag_filter: tuple[str, ...],
) -> None:
    """List issues for a project."""
    import json

    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)
    proj = get_project(config, project_id)

    if not proj:
        output_error("project_not_found", f"Project '{project_id}' not found", fmt=fmt)
        sys.exit(1)

    issues_file = proj.project_dir / ".agent" / "issues.jsonl"
    if not issues_file.exists():
        if fmt == OutputFormat.JSON:
            output_json({"issues": [], "count": 0})
        else:
            click.echo("No issues logged yet.")
        return

    tag_filter_set = set(tag_filter)
    issues = []
    with open(issues_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            issue = json.loads(line)
            if open_only and issue.get("fixed"):
                continue
            if issue_type and issue.get("type") != issue_type:
                continue
            if tag_filter_set and not (tag_filter_set & set(issue.get("tags", []))):
                continue
            issues.append(issue)

    if fmt == OutputFormat.JSON:
        output_json({"issues": issues, "count": len(issues)})
    else:
        if not issues:
            click.echo("No issues found.")
            return
        for i, issue in enumerate(issues, 1):
            status = "[OK]" if issue.get("fixed") else "[ ] "
            sev = issue.get("severity", "?")[0].upper()
            typ = issue.get("type", "?")
            tags = issue.get("tags") or []
            tag_str = f" [{','.join(tags)}]" if tags else ""
            desc = issue.get("summary") or issue.get("actual") or issue.get("context") or "No description"
            click.echo(f"{status} [{sev}] {typ}{tag_str}: {desc}")
