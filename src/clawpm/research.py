"""Research operations for ClawPM."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from .frontmatter import FrontmatterError, split_frontmatter
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

__all__ = [
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


def list_research(
    config: PortfolioConfig,
    project_id: str,
    status_filter: ResearchStatus | None = None,
    tags_filter: list[str] | None = None,
) -> list[Research]:
    """List all research items for a project."""
    research_dir = get_research_dir(config, project_id)
    if not research_dir or not research_dir.exists():
        return []

    items: list[Research] = []

    for file in research_dir.glob("*.md"):
        try:
            item = Research.from_file(file)

            # Apply status filter
            if status_filter is not None and item.status != status_filter:
                continue

            # Apply tags filter (must have ALL specified tags)
            if tags_filter:
                if not all(tag in item.tags for tag in tags_filter):
                    continue

            items.append(item)
        except Exception:
            # Skip malformed items
            continue

    # Sort by created date descending, then by ID
    items.sort(key=lambda r: (r.created or "", r.id), reverse=True)

    return items


def get_research(config: PortfolioConfig, project_id: str, research_id: str) -> Research | None:
    """Get a specific research item by ID."""
    research_dir = get_research_dir(config, project_id)
    if not research_dir or not research_dir.exists():
        return None

    # Check all files for matching ID
    for file in research_dir.glob("*.md"):
        try:
            item = Research.from_file(file)
            if item.id == research_id:
                return item
        except Exception:
            continue

    return None


def _render_open_body(question: str) -> str:
    """Progressive template for a genuinely open investigation (``--open``)."""
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
    open_ended: bool = False,
) -> Research | None:
    """Add a new research item to a project.

    Default is single-shot capture (verdict written into Summary/Findings/
    Conclusion at creation). Pass ``open_ended=True`` for the progressive
    template that keeps placeholder sections for an investigation filled in
    over time.

    Raises ValueError if single-shot capture is requested without a summary —
    the verdict is enforced at the library boundary, not just in the CLI, so a
    direct caller can't create a verdict-less entry that never reads as stale.
    """
    if not open_ended and not summary:
        raise ValueError(
            "single-shot research capture requires a summary (the verdict); "
            "pass open_ended=True for a progressive entry instead."
        )

    research_dir = get_research_dir(config, project_id)
    if not research_dir:
        return None

    # Create research directory if needed
    research_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()

    # Generate research ID if not provided
    if not research_id:
        # Use date + slugified title
        slug = title.lower()
        slug = "".join(c if c.isalnum() else "-" for c in slug)
        slug = "-".join(filter(None, slug.split("-")))[:50]
        slug = slug.rstrip("-")
        research_id = f"{project_id}-research-{slug}"

    # Build frontmatter
    frontmatter: dict = {
        "id": research_id,
        "type": research_type.value,
        "status": ResearchStatus.OPEN.value,
        "created": today,
    }

    if tags:
        frontmatter["tags"] = tags

    if open_ended:
        body = _render_open_body(question)
    else:
        body = _render_single_shot_body(question, summary, findings, conclusion)

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

    # Parse and update frontmatter — skip (return None) on any malformation.
    try:
        frontmatter, body = split_frontmatter(text)
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
