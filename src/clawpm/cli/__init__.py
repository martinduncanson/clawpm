"""ClawPM CLI — Filesystem-first multi-project manager.

This package is a thin registration shell (CLAWP-077). The root ``main`` click
group lives in :mod:`clawpm.cli.base`; each command group lives in its own
module under ``clawpm.cli`` and registers its commands onto ``main`` as an
import side effect. Importing every group module here assembles the full CLI,
and ``main`` is re-exported as the ``clawpm.cli:main`` console-script entry
point.
"""

from __future__ import annotations

import os
import sys

# Imported for its module object, not for direct use here: tests patch
# ``clawpm.cli.subprocess.run`` (a shared stdlib module) to spy on the git
# subprocesses run by commands now living in sibling modules.
import subprocess  # noqa: F401


# cp1252-safe stdio (CLAWP-011): Windows consoles default to the cp1252 codec,
# which cannot encode glyphs such as U+2192 and raises UnicodeEncodeError
# mid-render. Reconfigure stdout/stderr to UTF-8 (errors="replace") so NO output
# path -- echo args, --help text, command docstrings, tabulated rows -- can
# crash, regardless of which glyph a future line introduces. This runs at
# package import, before any group module loads, so the whole CLI is covered.
# It is the root-cause fix the encoding_check scanner recommends; it supersedes
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


# Root group (re-exported as the clawpm.cli:main console-script entry point).
from clawpm.cli.base import main  # noqa: E402,F401

# --- group module registrations (import each for its command-registration side effect) ---
from clawpm.cli import agent as _agent  # noqa: E402,F401
from clawpm.cli import hook as _hook  # noqa: E402,F401
from clawpm.cli import judge as _judge  # noqa: E402,F401
from clawpm.cli import research as _research  # noqa: E402,F401
from clawpm.cli import mission as _mission  # noqa: E402,F401
from clawpm.cli import lease as _lease  # noqa: E402,F401
from clawpm.cli import issues as _issues  # noqa: E402,F401
from clawpm.cli import conflicts as _conflicts  # noqa: E402,F401
from clawpm.cli import inbox as _inbox  # noqa: E402,F401
from clawpm.cli import constitution as _constitution  # noqa: E402,F401
from clawpm.cli import serve as _serve  # noqa: E402,F401
from clawpm.cli import reflect as _reflect  # noqa: E402,F401
from clawpm.cli import log as _log  # noqa: E402,F401
from clawpm.cli import tasks as _tasks  # noqa: E402,F401
from clawpm.cli import projects as _projects  # noqa: E402,F401
from clawpm.cli import shortcuts as _shortcuts  # noqa: E402,F401
from clawpm.cli import project as _project  # noqa: E402,F401
from clawpm.cli import admin as _admin  # noqa: E402,F401
from clawpm.cli import resume as _resume  # noqa: E402,F401
from clawpm.cli import use as _use  # noqa: E402,F401

# Re-exports: symbols that moved into group modules but are still referenced
# via the historical `clawpm.cli.<name>` path (by the domain layer and tests).
from clawpm.cli.conflicts import _globs_overlap  # noqa: E402,F401
from clawpm.cli.serve import _load_web_server  # noqa: E402,F401
