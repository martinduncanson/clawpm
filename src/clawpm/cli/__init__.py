"""ClawPM CLI - Filesystem-first multi-project manager."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import click

from clawpm import __version__
from clawpm.concurrency import LockTimeout
from clawpm.frontmatter import parse_frontmatter
from clawpm.models import (
    ProjectStatus,
    SuccessCriterion,
    Task,
    TaskState,
    TaskComplexity,
    WorkLogAction,
    ResearchType,
    ResearchStatus,
    Predictions,
    SURPRISE_TAXONOMY,
)
from clawpm.output import (
    OutputFormat,
    output_json,
    output_error,
    output_success,
    output_projects_list,
    output_tasks_list,
    output_task_detail,
    output_worklog_entries,
    output_research_list,
    output_context,
)
from clawpm.discovery import (
    get_portfolio_path,
    load_portfolio_config,
    discover_projects,
    discover_untracked_repos,
    get_project,
    validate_portfolio,
    init_project_from_repo,
    is_git_repo,
    path_for_config,
)
from clawpm.announce import (
    AnnounceEncodingError,
    find_existing_marker_file,
    select_target_file,
    write_or_replace_stanza,
)
from clawpm.tasks import (
    list_tasks,
    get_task,
    get_next_task,
    change_task_state,
    add_task,
    edit_task,
    split_task,
    add_subtask,
    touch_task_updated,
    distinct_tags,
)
from clawpm.worklog import (
    add_entry,
    filter_files_changed,
    tail_entries,
    get_last_entry,
    get_logged_commit_hashes,
    read_entries,
)
from clawpm.research import (
    list_research,
    get_research,
    add_research,
    link_research_session,
)
from clawpm.context import (
    resolve_project,
    expand_task_id,
    get_context_project,
    set_context_project,
    detect_project_from_cwd,
    detect_untracked_repo_from_cwd,
    auto_init_if_untracked,
)


# cp1252-safe stdio (CLAWP-011): Windows consoles default to the cp1252 codec,
# which cannot encode glyphs such as U+2192 and raises UnicodeEncodeError
# mid-render. Reconfigure stdout/stderr to UTF-8 (errors="replace") so NO output
# path -- echo args, --help text, command docstrings, tabulated rows -- can
# crash, regardless of which glyph a future line introduces. This is the
# root-cause fix the encoding_check scanner recommends; it supersedes
# whack-a-mole glyph swapping. Guarded because redirected / wrapped streams
# (e.g. click's CliRunner, a closed pipe) may lack reconfigure() or reject it.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError) as _stdio_exc:  # pragma: no cover
    # Don't crash at import over a display-only setting. Stay SILENT by default:
    # the common failure is a benign wrapped/redirected stream (CliRunner, a
    # pipe) that legitimately lacks reconfigure(), and surfacing it would
    # false-alarm on every piped run. Under CLAWPM_DEBUG, leave a breadcrumb so a
    # genuine cp1252 console that refused UTF-8 is debuggable, not a mystery
    # UnicodeEncodeError three lines later (fail-open != fail-silent).
    if os.environ.get("CLAWPM_DEBUG"):
        sys.stderr.write(
            f"clawpm: {__name__} stdio reconfigure to utf-8 failed "
            f"({_stdio_exc!r}); non-ASCII output may crash on a cp1252 console\n"
        )


from clawpm.cli.base import (
    main,
    _mutation_errors,
    get_format,
    require_portfolio,
    require_project,
    _read_patterns_file,
    pass_format,
    _FALLBACK_POLICIES,
)

# --- group module registrations (import each for its command-registration side effect) ---
from clawpm.cli import agent as _agent  # noqa: F401 (registers commands)
from clawpm.cli import hook as _hook  # noqa: F401 (registers commands)
from clawpm.cli import judge as _judge  # noqa: F401 (registers commands)
from clawpm.cli import research as _research  # noqa: F401 (registers commands)
from clawpm.cli import mission as _mission  # noqa: F401 (registers commands)
from clawpm.cli import lease as _lease  # noqa: F401 (registers commands)
from clawpm.cli import issues as _issues  # noqa: F401 (registers commands)
from clawpm.cli import conflicts as _conflicts  # noqa: F401 (registers commands)
from clawpm.cli import inbox as _inbox  # noqa: F401 (registers commands)
from clawpm.cli import constitution as _constitution  # noqa: F401 (registers commands)
from clawpm.cli import serve as _serve  # noqa: F401 (registers commands)
from clawpm.cli import reflect as _reflect  # noqa: F401 (registers commands)
from clawpm.cli import log as _log  # noqa: F401 (registers commands)
from clawpm.cli import tasks as _tasks  # noqa: F401 (registers commands)
from clawpm.cli import projects as _projects  # noqa: F401 (registers commands)
from clawpm.cli import shortcuts as _shortcuts  # noqa: F401 (registers commands)
from clawpm.cli import project as _project  # noqa: F401 (registers commands)
from clawpm.cli import admin as _admin  # noqa: F401 (registers commands)
from clawpm.cli import resume as _resume  # noqa: F401 (registers commands)
from clawpm.cli import use as _use  # noqa: F401 (registers commands)

# Re-exports: symbols that moved into group modules but are still referenced
# via the historical `clawpm.cli.<name>` path (by the domain layer and tests).
from clawpm.cli.conflicts import _globs_overlap  # noqa: F401
from clawpm.cli.serve import _load_web_server  # noqa: F401
