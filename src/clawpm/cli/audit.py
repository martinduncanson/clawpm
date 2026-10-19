from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from clawpm.audit_findings import (
    DEFAULT_MAX_FINDINGS,
    DIMENSIONS,
    AuditFindingError,
    build_emit_document,
    parse_findings,
)
from clawpm.cli.base import main, get_format, ExpandedPath
from clawpm.output import output_error

# ============================================================================
# Audit subcommands — deterministic door to the project-health audit core (CLAWP-099)
# ============================================================================


@main.group()
def audit() -> None:
    """Project-health audit primitives (no agents, no LLM).

    The clawpm-planner skill fans out the dimension agents; this group only
    validates, dedups and maps what they returned.
    """
    pass


def _read_dimension_files(
    findings_dir: Path, dimensions_run: list[str]
) -> tuple[dict[str, Any], list[str]]:
    """Load ``<dimension>.json`` files; return (by_dimension, problems)."""
    problems: list[str] = []
    by_dimension: dict[str, Any] = {}
    files = {p.stem: p for p in sorted(findings_dir.glob("*.json")) if p.is_file()}
    for dim in dimensions_run:
        path = files.get(dim)
        if path is None:
            problems.append(
                f"{dim}: dispatched but no findings file {dim}.json in {findings_dir} "
                "(a dimension that returned nothing must write an explicit [])"
            )
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            problems.append(f"{path.name}: cannot read as JSON ({exc})")
            continue
        if not isinstance(raw, list):
            problems.append(f"{path.name}: must be a JSON list of findings")
            continue
        by_dimension[dim] = raw
    for stem, path in files.items():
        if stem not in dimensions_run:
            problems.append(f"{path.name}: dimension {stem!r} is not in --dimensions-run")
    return by_dimension, problems


@audit.command("collate")
@click.option(
    "--findings-dir", "findings_dir",
    type=ExpandedPath(exists=True, file_okay=False), required=True,
    help="Directory holding one <dimension>.json file (a JSON list of findings) per dispatched dimension.",
)
@click.option(
    "--dimensions-run", "dimensions_run", required=True,
    help="Comma-separated dimensions the orchestrator DISPATCHED. A missing file for one of "
         "these fails loudly; an explicit [] is a valid empty result.",
)
@click.option("--root-title", "root_title", required=True, help="Title of the emitted root task.")
@click.option(
    "--max-leaves", "max_leaves", type=int, default=DEFAULT_MAX_FINDINGS, show_default=True,
    help="Cap on emitted leaves; dropped findings are named in the report.",
)
@click.pass_context
def audit_collate(
    ctx: click.Context,
    findings_dir: str,
    dimensions_run: str,
    root_title: str,
    max_leaves: int,
) -> None:
    """Validate, dedup and map dimension-agent findings to an emit-tree document.

    Prints JSON on stdout: {nothing_to_emit, document, report}. Pipe
    ``document`` (when not null) to ``clawpm tasks emit-tree``. Read-only: no
    LLM or network calls, and nothing is written unless you redirect stdout.
    """
    fmt = get_format(ctx)
    if max_leaves < 1:
        output_error("invalid_max_leaves", "--max-leaves must be >= 1.", fmt=fmt)
        sys.exit(1)

    dims = list(dict.fromkeys(d.strip() for d in dimensions_run.split(",") if d.strip()))
    if not dims:
        output_error("no_dimensions", "--dimensions-run named no dimensions.", fmt=fmt)
        sys.exit(1)
    unknown = [d for d in dims if d not in DIMENSIONS]
    if unknown:
        output_error(
            "audit_collate_failed",
            f"unknown dimension(s) in --dimensions-run: {unknown} (expected from {list(DIMENSIONS)})",
            details={"problems": [f"unknown dimension {d!r}" for d in unknown]},
            fmt=fmt,
        )
        sys.exit(1)

    by_dimension, problems = _read_dimension_files(Path(findings_dir), dims)
    findings = []
    if not problems:
        try:
            findings = parse_findings(by_dimension)
        except AuditFindingError as exc:
            problems = str(exc).splitlines()
    if problems:
        output_error(
            "audit_collate_failed",
            f"{len(problems)} problem(s) collating findings:\n- " + "\n- ".join(problems),
            details={"problems": problems},
            fmt=fmt,
        )
        sys.exit(1)

    doc, report = build_emit_document(findings, root_title, max_findings=max_leaves)
    report["dimensions_run"] = dims
    report["findings_per_dimension"] = {d: len(by_dimension[d]) for d in dims}
    click.echo(json.dumps(
        {"nothing_to_emit": doc is None, "document": doc, "report": report}, indent=2
    ))
