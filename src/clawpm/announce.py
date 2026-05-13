"""Project-requirement announcement helper.

When a repository is tracked by clawpm, agents picking up the repo should see
a clear "this project uses clawpm" stanza in the first agent-facing doc they
read. This module writes/replaces an HTML-marker-bounded block in
``CLAUDE.md``, ``AGENTS.md``, or ``README.md`` (first-found wins) so the
marker is idempotent — re-running announce replaces in-place rather than
appending duplicates.

Used by:
- ``clawpm project init`` (auto-runs announce at end of bootstrap)
- ``clawpm project announce`` (operator-callable on demand)
- ``clawpm doctor`` (Check e — warns when no marker is present anywhere)
"""

from __future__ import annotations

from pathlib import Path

# Precedence order: first file found in the repo wins. Targets the doc the
# agent harness is most likely to load (Claude Code, OpenAI Codex, generic).
AGENT_DOCS_PRECEDENCE = ("CLAUDE.md", "AGENTS.md", "README.md")

# HTML comment markers — agent docs are markdown, so HTML comments render
# as invisible but remain in source. The exact strings are the contract;
# don't change without a migration plan.
MARKER_START = "<!-- clawpm:project-requirement -->"
MARKER_END = "<!-- /clawpm:project-requirement -->"


def generate_stanza(project_id: str, project_name: str | None = None) -> str:
    """Return the marker-bounded stanza for a project.

    The body lists the canonical clawpm commands an agent needs to know
    when working in the repo. Keep it short — agents under context pressure
    skip long blocks.
    """
    display = project_name or project_id
    return f"""{MARKER_START}
## Project management — clawpm

This project ({display}) uses [clawpm](https://github.com/martinduncanson/clawpm) for task tracking and calibration capture. Any agent working in this repo MUST use the existing PM structure rather than improvising:

- `clawpm tasks list --project {project_id}` — see open tasks before starting work.
- `clawpm tasks add --project {project_id} --predict-*` — file new work with predictions (duration, complexity, success-criteria, pre-mortem). Use `--predicted-by agent` and ask the operator to confirm.
- `clawpm log` after substantive work; `clawpm log commit` after each commit to populate the work_log.
- `.project/SPEC.md` is the project scope; `.project/tasks/` is the live backlog; `.project/notes/` holds operator-facing notes (read these before starting).

If `clawpm` is not on PATH: `pipx install git+https://github.com/martinduncanson/clawpm` (then `clawpm doctor` to verify).
{MARKER_END}"""


def find_existing_marker_file(repo_path: Path) -> Path | None:
    """Return the first agent-doc in the repo that already contains the marker,
    or None if no agent-doc has it. Used by doctor Check e."""
    for fname in AGENT_DOCS_PRECEDENCE:
        f = repo_path / fname
        if f.exists():
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if MARKER_START in content:
                return f
    return None


def select_target_file(repo_path: Path) -> Path:
    """Pick which doc to write to. First existing file in precedence order wins;
    if none exist, default to CLAUDE.md (will be created)."""
    for fname in AGENT_DOCS_PRECEDENCE:
        f = repo_path / fname
        if f.exists():
            return f
    return repo_path / AGENT_DOCS_PRECEDENCE[0]


def write_or_replace_stanza(
    repo_path: Path,
    project_id: str,
    project_name: str | None = None,
) -> tuple[Path, str]:
    """Write the announce stanza into the repo's agent docs.

    Returns ``(target_file, action)`` where ``action`` is one of:
      - ``"created"`` — file didn't exist; new file written with stanza only.
      - ``"replaced"`` — existing marker block found; replaced in-place.
      - ``"appended"`` — file existed without a marker block; stanza appended.

    Idempotent: re-running on a file that already has the marker block
    rewrites between markers, leaves surrounding content untouched.
    """
    target = select_target_file(repo_path)
    stanza = generate_stanza(project_id, project_name)

    if not target.exists():
        target.write_text(stanza + "\n", encoding="utf-8")
        return target, "created"

    content = target.read_text(encoding="utf-8", errors="replace")

    if MARKER_START in content and MARKER_END in content:
        start_idx = content.index(MARKER_START)
        end_idx = content.index(MARKER_END) + len(MARKER_END)
        new_content = content[:start_idx] + stanza + content[end_idx:]
        target.write_text(new_content, encoding="utf-8")
        return target, "replaced"

    # File exists but no marker — append with a leading blank line for separation.
    separator = "" if content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
    target.write_text(content + separator + stanza + "\n", encoding="utf-8")
    return target, "appended"
