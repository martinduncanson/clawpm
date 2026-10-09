"""Research operations for ClawPM."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .concurrency import file_lock
from .frontmatter import FrontmatterError, parse_frontmatter, split_frontmatter
from .models import (
    Research,
    ResearchType,
    ResearchStatus,
    PortfolioConfig,
    PLACEHOLDER_STALE_DAYS,
    has_placeholder_sections,
    is_stale_placeholder,
)
from .discovery import get_project_dir

logger = logging.getLogger(__name__)

__all__ = [
    "ResearchScan",
    "scan_research",
    "PLACEHOLDER_STALE_DAYS",
    "has_placeholder_sections",
    "is_stale_placeholder",
    "get_research_dir",
    "list_research",
    "get_research",
    "add_research",
    "link_research_session",
]


def get_research_dir(config: PortfolioConfig, project_id: str) -> Path | None:
    """Get the research directory for a project."""
    project_dir = get_project_dir(config, project_id)
    if project_dir:
        research_dir = project_dir / "research"
        return research_dir
    return None


@dataclass
class ResearchScan:
    """Result of scanning a research dir: parsed items plus unreadable files.

    ``malformed`` entries are ``{"file", "file_path", "reason", "message"}``.
    """

    items: list[Research] = field(default_factory=list)
    malformed: list[dict[str, str]] = field(default_factory=list)


def _malformed_entry(file: Path, exc: Exception) -> dict[str, str]:
    reason = getattr(exc, "reason", None) or type(exc).__name__
    return {
        "file": file.name,
        "file_path": str(file),
        "reason": str(reason),
        "message": str(exc),
    }


def scan_research(
    config: PortfolioConfig,
    project_id: str,
    status_filter: ResearchStatus | None = None,
    tags_filter: list[str] | None = None,
) -> ResearchScan:
    """Scan a project's research files, surfacing (not dropping) bad ones.

    Malformed files are reported regardless of ``status_filter``/``tags_filter``
    (their status/tags cannot be read), and each one is logged at WARNING.
    """
    scan = ResearchScan()
    research_dir = get_research_dir(config, project_id)
    if not research_dir or not research_dir.exists():
        return scan

    for file in sorted(research_dir.glob("*.md")):
        try:
            item = Research.from_file(file)
        except Exception as exc:  # noqa: BLE001 - recorded, never dropped
            entry = _malformed_entry(file, exc)
            scan.malformed.append(entry)
            logger.warning("malformed research file skipped: %s (%s)", file, entry["reason"])
            continue

        try:
            if status_filter is not None and item.status != status_filter:
                continue
            if tags_filter and not all(tag in item.tags for tag in tags_filter):
                continue
        except Exception as exc:  # noqa: BLE001 - recorded, never crash the listing
            entry = _malformed_entry(file, exc)
            scan.malformed.append(entry)
            logger.warning("research file skipped while filtering: %s (%s)", file, entry["reason"])
            continue
        scan.items.append(item)

    # Sort by created date descending, then by ID
    scan.items.sort(key=lambda r: (r.created or "", r.id), reverse=True)
    return scan


def list_research(
    config: PortfolioConfig,
    project_id: str,
    status_filter: ResearchStatus | None = None,
    tags_filter: list[str] | None = None,
) -> list[Research]:
    """List research items (flat list; malformed files are logged, see
    :func:`scan_research` to get them back as data)."""
    return scan_research(config, project_id, status_filter, tags_filter).items


def get_research(config: PortfolioConfig, project_id: str, research_id: str) -> Research | None:
    """Get a specific research item by ID (malformed files are logged, not matched)."""
    research_dir = get_research_dir(config, project_id)
    if not research_dir or not research_dir.exists():
        return None

    for file in research_dir.glob("*.md"):
        try:
            item = Research.from_file(file)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "malformed research file skipped while looking up %r: %s (%s)",
                research_id, file, getattr(exc, "reason", type(exc).__name__),
            )
            continue
        if item.id == research_id:
            return item

    return None


def _existing_ids(research_dir: Path) -> set[str]:
    """Effective ids of every research file (same derivation as the reader).

    Unreadable/corrupt files cannot be reserved, so each is logged at WARNING.
    """
    ids: set[str] = set()
    for file in research_dir.glob("*.md"):
        try:
            eid = Research.peek_id(file)
        except Exception as exc:  # noqa: BLE001 - logged, never silent
            logger.warning(
                "research file skipped during id allocation (its id is not reserved): %s (%s)",
                file, getattr(exc, "reason", type(exc).__name__),
            )
            continue
        if eid is not None:
            ids.add(eid)
    return ids


def _render_open_body(question: str) -> str:
    """Progressive template for a genuinely open investigation (no verdict yet)."""
    return f"""## Question

{question or "(Describe the research question)"}

## Summary

(To be filled in as research progresses)

## Findings

...

## Conclusion

...
"""


def _render_single_shot_body(
    question: str,
    summary: str,
    findings: list[str] | None,
    conclusion: str,
) -> str:
    """Single-shot capture: verdict recorded at creation, no rotting stubs.

    Only sections with real content are emitted — an empty Findings/Conclusion
    is omitted rather than stubbed, so the placeholder detector stays clean.
    """
    sections: list[str] = []
    if question:
        sections.append(f"## Question\n\n{question}")
    sections.append(f"## Summary\n\n{summary}")
    if findings:
        bullets = "\n".join(f"- {f}" for f in findings)
        sections.append(f"## Findings\n\n{bullets}")
    if conclusion:
        sections.append(f"## Conclusion\n\n{conclusion}")
    return "\n\n".join(sections) + "\n"


def add_research(
    config: PortfolioConfig,
    project_id: str,
    title: str,
    research_type: ResearchType,
    research_id: str | None = None,
    tags: list[str] | None = None,
    question: str = "",
    summary: str = "",
    findings: list[str] | None = None,
    conclusion: str = "",
) -> Research | None:
    """Add a new research item to a project.

    The template is inferred from the content supplied: if a summary, findings,
    or conclusion is given, the verdict is written straight into those sections
    (single-shot capture); otherwise a progressive template with placeholder
    sections is emitted for an investigation to be filled in over time. A
    progressive entry still carries the stub markers, so it surfaces via the
    staleness signal if it's never completed.

    The verdict-or-explicit-open contract is enforced at the CLI boundary
    (``research add``), not here, so direct/library callers stay flexible.
    """
    research_dir = get_research_dir(config, project_id)
    if not research_dir:
        return None

    # Create research directory if needed
    research_dir.mkdir(parents=True, exist_ok=True)

    # Scan -> allocate -> write is one critical section (CLAWP-051/066/067
    # file_lock, reentrant per-thread): two writers must not both scan the same
    # ids and mint the same one. file_lock needs an absolute path; LockTimeout
    # propagates (as in tasks.py). The sentinel is not *.md so scans ignore it.
    with file_lock(research_dir.resolve() / ".clawpm-research.lock"):
        today = date.today().isoformat()

        # Generate research ID if not provided
        if not research_id:
            # Use date + slugified title
            slug = title.lower()
            slug = "".join(c if c.isalnum() else "-" for c in slug)
            slug = "-".join(filter(None, slug.split("-")))[:50]
            slug = slug.rstrip("-")
            base_id = f"{project_id}-research-{slug}"
            # Unique the frontmatter id (not just the filename): get_research
            # resolves by id, so a collision would shadow the later entry.
            taken = _existing_ids(research_dir)
            research_id = base_id
            n = 2
            while research_id in taken:
                research_id = f"{base_id}-{n}"
                n += 1

        # Build frontmatter
        frontmatter: dict = {
            "id": research_id,
            "type": research_type.value,
            "status": ResearchStatus.OPEN.value,
            "created": today,
        }

        if tags:
            frontmatter["tags"] = tags

        # Single-shot when a verdict/content is supplied; progressive otherwise.
        if summary or findings or conclusion:
            body = _render_single_shot_body(question, summary, findings, conclusion)
        else:
            body = _render_open_body(question)

        # Build content
        content = f"""---
{yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True).strip()}
---
# {title}

{body}"""

        # Generate filename
        filename = f"{today}_{research_id.replace(f'{project_id}-research-', '')}.md"
        file_path = research_dir / filename

        # Ensure unique filename
        counter = 1
        while file_path.exists():
            filename = f"{today}_{research_id.replace(f'{project_id}-research-', '')}_{counter}.md"
            file_path = research_dir / filename
            counter += 1

        file_path.write_text(content, encoding="utf-8")

        return Research.from_file(file_path)


def link_research_session(
    config: PortfolioConfig,
    project_id: str,
    research_id: str,
    session_key: str,
    run_id: str | None = None,
    spawned_by: str | None = None,
) -> Research | None:
    """Link a research item to an OpenClaw session."""
    item = get_research(config, project_id, research_id)
    if not item or not item.file_path:
        return None

    # Read current content
    text = item.file_path.read_text(encoding="utf-8")

    # Parse and update frontmatter — skip (return None) on any malformation,
    # including "not_a_mapping". Tried distinguishing not_a_mapping to raise
    # here (antigravity review, CLAWP-091), then reverted: it's unreachable.
    # get_research() above resolves items by comparing the FRONTMATTER's own
    # `id` field to research_id (research filenames are date-prefixed, not
    # the research_id itself — unlike tasks, where get_task() locates by
    # filename and corrupted frontmatter doesn't block the lookup). Once a
    # research item's frontmatter is corrupted into a non-mapping, its `id`
    # is unrecoverable, so get_research() can never match it and this
    # function's own `if not item: return None` fires first — the guard
    # below would be dead code. Fixing this needs a different, filename-
    # aware lookup in get_research() (a structurally separate problem from
    # CLAWP-091's mutation-site guard, and out of scope here).
    try:
        frontmatter, body = split_frontmatter(text, where=str(item.file_path))
    except FrontmatterError:
        return None

    # Add openclaw section
    frontmatter["openclaw"] = {
        "child_session_key": session_key,
        "spawned_at": date.today().isoformat(),
    }
    if run_id:
        frontmatter["openclaw"]["run_id"] = run_id
    if spawned_by:
        frontmatter["openclaw"]["spawned_by"] = spawned_by

    # Update status
    frontmatter["status"] = "in-progress"

    # Rebuild content
    new_content = f"""---
{yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True).strip()}
---{body}"""

    item.file_path.write_text(new_content, encoding="utf-8")

    return Research.from_file(item.file_path)
