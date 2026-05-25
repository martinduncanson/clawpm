"""Heuristic check for cp1252-risk patterns in Python source files (CLAWP-011).

Tooling-rule enforcement of feedback-windows-cp1252-write-text.md. The memory's
sentinel said: "if a 5th instance lands, escalate from 'discipline rule' to
'tooling rule' — write a clawpm doctor check that grep-scans for non-ASCII
chars in click.echo / print lines and for strict encoding='utf-8' reads
without errors=". 6 confirmed incidents 2026-05-08 to 2026-05-21 triggered
the escalation.

Three checks, all run together when `clawpm doctor --check-encoding` fires:

1. Non-ASCII literals in print() / click.echo() args
   → crashes on Windows cp1252 stdout
2. open() / Path.read_text() / Path.write_text() / Path.open() without
   explicit encoding= kwarg
   → silent platform-default encoding (cp1252 on Windows) crashes on UTF-8
3. Modules with print/click.echo calls but no sys.stdout.reconfigure() at
   module top
   → catches the runtime-variable case (apply.py case #6) where the literal
   is ASCII but the formatted output may contain non-ASCII from data

AST-based detection so we get the actual call structure, not regex false
positives in string literals or comments.
"""

from __future__ import annotations

import ast
from pathlib import Path


PRINT_LIKE_NAMES = {"print"}
"""Bare names that act as stdout-writers."""

PRINT_LIKE_ATTRS = {"echo", "secho", "print", "write"}
"""Attribute names that act as stdout-writers when the receiver is one of
PRINT_LIKE_RECEIVERS (covers click.echo, click.secho, typer.echo,
sys.stdout.write, sys.stderr.write)."""

PRINT_LIKE_RECEIVERS = {"click", "typer", "sys"}
"""Receiver names whose .echo/.print/.secho calls actually hit stdout. Tightens
the match so domain objects with .print/.echo methods (logger.echo, Rich's
console.print, model.print) don't false-positive."""

TEXT_FILE_OP_ATTRS = {"read_text", "write_text"}
"""Pathlib methods that take an encoding= kwarg — unambiguous (no other API
uses these names with file-text semantics)."""

BYTES_FILE_OP_ATTRS = {"read_bytes", "write_bytes"}
"""Pathlib bytes methods; tracked separately because they don't take encoding=."""

PATHLIB_RECEIVER_NAMES = {
    "Path", "PurePath", "PurePosixPath", "PureWindowsPath",
    "PosixPath", "WindowsPath",
}
"""Pathlib class names whose `.open()` is unambiguously a text file op.

Codex PR#5 round-1 P1: any `.open()` attribute call matched as a file op,
which false-positives on `zipfile.ZipFile(p).open(...)`, `tarfile.open(...)`,
`socket.create_connection(...).open(...)`, etc. Narrow `.open` matching to
calls whose receiver is provably a pathlib Path — Call to a pathlib name
like `Path(p).open(...)` or `pathlib.Path(p).open(...)`. The bare `open(...)`
builtin remains broadly matched."""


def _is_nonascii(s: str) -> bool:
    """Return True if string contains any character outside ASCII."""
    try:
        s.encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def _string_literals_in_node(node: ast.AST) -> list[str]:
    """Collect all string literal values reachable inside an AST node.

    Walks Constant (str), JoinedStr (f-strings — pulls out the literal parts
    but not the runtime expressions inside {...}), and FormattedValue children.
    """
    literals: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            literals.append(child.value)
        elif isinstance(child, ast.JoinedStr):
            for value in child.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    literals.append(value.value)
    return literals


def _is_print_like_call(node: ast.Call) -> bool:
    """Return True if this Call is a print() or click.echo()-shaped call.

    For attribute calls (foo.echo(...)), require the receiver to be a Name in
    PRINT_LIKE_RECEIVERS, or an Attribute whose root is `sys` (e.g.
    sys.stdout.write). This avoids false positives on domain objects with
    their own .echo/.print/.secho methods (logger.echo, Rich's console.print,
    SQLAlchemy's model.print, etc.).
    """
    func = node.func
    if isinstance(func, ast.Name) and func.id in PRINT_LIKE_NAMES:
        return True
    if isinstance(func, ast.Attribute) and func.attr in PRINT_LIKE_ATTRS:
        receiver = func.value
        if isinstance(receiver, ast.Name) and receiver.id in PRINT_LIKE_RECEIVERS:
            return True
        # sys.stdout.X / sys.stderr.X — walk down to the sys root
        cursor = receiver
        while isinstance(cursor, ast.Attribute):
            cursor = cursor.value
        if isinstance(cursor, ast.Name) and cursor.id == "sys":
            return True
    return False


def _is_pathlib_receiver(node: ast.expr) -> bool:
    """True if expr is a Call to a pathlib class — `Path(p)`, `pathlib.Path(p)`,
    `PurePath(p)`, etc.

    Used to narrow `.open()` matching so zipfile/tarfile/socket-style
    `.open()` calls don't false-positive. Conservative: only direct Call
    nodes count, since a Name like `p` could be any type at static-analysis
    time (we'd miss `p = Path(x); p.open()` — accepted as a known
    detection gap, captured in the concerns block).
    """
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Name) and f.id in PATHLIB_RECEIVER_NAMES:
        return True
    if isinstance(f, ast.Attribute) and f.attr in PATHLIB_RECEIVER_NAMES:
        return True
    return False


def _is_file_op_call(node: ast.Call) -> tuple[bool, str]:
    """Return (is_file_op, name) for the call.

    Detects:
      - `open(...)` — bare Name call (builtin)
      - `<receiver>.read_text(...)` / `.write_text(...)` — unambiguous pathlib
      - `<receiver>.read_bytes(...)` / `.write_bytes(...)` — pathlib bytes
      - `Path(p).open(...)` etc. — pathlib `.open` only matches when receiver
        is provably a pathlib class Call (Codex PR#5 round-1 P1 fix)

    Returns (False, "") for `zipfile.ZipFile(p).open(...)`,
    `tarfile.open(...)`, `socket.open(...)`, generic `obj.open(...)` —
    these used to false-positive as missing-encoding=.
    """
    func = node.func
    if isinstance(func, ast.Name) and func.id == "open":
        return True, "open"
    if isinstance(func, ast.Attribute):
        if func.attr in TEXT_FILE_OP_ATTRS or func.attr in BYTES_FILE_OP_ATTRS:
            return True, func.attr
        if func.attr == "open" and _is_pathlib_receiver(func.value):
            return True, "open"
    return False, ""


def _has_encoding_kwarg(node: ast.Call) -> bool:
    """Return True if any keyword arg is named 'encoding', OR if the call
    forwards **kwargs (kw.arg is None on a DoubleStarred unpack).

    The **kwargs case is a deliberate false-negative: wrapper functions
    that forward kwargs through to open()/read_text()/write_text() may
    legitimately have encoding= flowing in via the dict. Flagging every
    such wrapper would generate noise on what is in fact a correct
    forwarding pattern. Operators who genuinely forget encoding= in a
    wrapper still get caught at the call site that passes the kwargs.
    """
    for kw in node.keywords:
        if kw.arg is None or kw.arg == "encoding":
            return True
    return False


def _has_binary_mode(node: ast.Call) -> bool:
    """For open()/Path.open() calls, return True if the mode string contains 'b'.

    Binary-mode reads/writes don't take encoding=. Without this check, every
    `open(path, "rb")` would false-positive on missing-encoding-kwarg.

    Handles both call shapes:
    - `open(path, mode, ...)` (Name call) — mode is positional arg index 1
    - `Path(p).open(mode, ...)` (Attribute call) — self-bound, mode is index 0
    """
    if isinstance(node.func, ast.Name):
        mode_index = 1  # builtin open: open(file, mode, ...)
    else:
        mode_index = 0  # Path.open / file.open: mode is first arg after receiver

    if len(node.args) > mode_index:
        mode_node = node.args[mode_index]
        if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
            if "b" in mode_node.value:
                return True
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            if "b" in kw.value.value:
                return True
    return False


def _is_stdout_reconfigure_call(stmt: ast.stmt) -> bool:
    """Return True if stmt is an Expr wrapping `sys.stdout.reconfigure(...)`.

    Codex PR#5 round-1 P2 fix: previously this matched any
    `<...>.stdout.reconfigure(...)` shape — including
    `process.stdout.reconfigure(...)` on a subprocess handle, which doesn't
    protect the host stdout. Now the chain must bottom out at the Name
    `sys` (matches `sys.stdout.reconfigure(...)` exactly).
    """
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    call = stmt.value
    if not (isinstance(call.func, ast.Attribute)
            and call.func.attr == "reconfigure"
            and isinstance(call.func.value, ast.Attribute)
            and call.func.value.attr == "stdout"):
        return False
    # call.func.value is the `<X>.stdout` Attribute; its `.value` must be Name('sys').
    return (
        isinstance(call.func.value.value, ast.Name)
        and call.func.value.value.id == "sys"
    )


def _has_stdout_reconfigure(tree: ast.Module) -> bool:
    """Return True if the module's top-level statements include a
    sys.stdout.reconfigure(...) call.

    Conservative: only looks at module-level statements (and one-level-deep
    inside `if hasattr(...)` or `try:` guards — the two common defensive
    patterns). Anything more nuanced (nested in a function, class, or
    deeper conditional) is a guard the operator has made deliberately, and
    we still flag the module.
    """
    for stmt in tree.body:
        if _is_stdout_reconfigure_call(stmt):
            return True
        # Common guard: `if hasattr(sys.stdout, "reconfigure"): ...`
        if isinstance(stmt, ast.If):
            for body_stmt in stmt.body:
                if _is_stdout_reconfigure_call(body_stmt):
                    return True
        # Common guard: `try: sys.stdout.reconfigure(...) except AttributeError: pass`
        if isinstance(stmt, ast.Try):
            for body_stmt in stmt.body:
                if _is_stdout_reconfigure_call(body_stmt):
                    return True
    return False


def _has_print_like_call_anywhere(tree: ast.Module) -> bool:
    """Return True if the module contains any print/click.echo Call."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_print_like_call(node):
            return True
    return False


def check_file(file_path: Path) -> list[dict]:
    """Run all three checks against one .py file. Return a list of finding dicts.

    Each finding: {
        "file": "<posix-path>",
        "line": <int>,
        "rule": "nonascii-in-print" | "missing-encoding-kwarg" | "unconfigured-stdout",
        "evidence": "<short description with code snippet or context>",
    }
    """
    findings: list[dict] = []
    posix = file_path.as_posix()
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Surface (don't swallow): the scanner's animating principle is that
        # the operator deserves to see files the tooling can't open.
        findings.append({
            "file": posix,
            "line": 0,
            "rule": "unreadable-source",
            "evidence": f"could not read file: {exc}",
        })
        return findings
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        # A tracked .py file that won't parse is itself portfolio-health signal.
        findings.append({
            "file": posix,
            "line": exc.lineno or 0,
            "rule": "unparseable-source",
            "evidence": f"syntax error: {exc.msg}",
        })
        return findings

    # Check 1: non-ASCII in print/click.echo calls
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_print_like_call(node):
            continue
        for literal in _string_literals_in_node(node):
            if _is_nonascii(literal):
                findings.append({
                    "file": posix,
                    "line": node.lineno,
                    "rule": "nonascii-in-print",
                    "evidence": f"print/echo arg contains non-ASCII: {literal!r}",
                })
                break  # one finding per call site is enough

    # Check 2: file ops missing encoding= kwarg
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_op, op_name = _is_file_op_call(node)
        if not is_op:
            continue
        # bytes variants don't take encoding=; skip them
        if op_name in {"read_bytes", "write_bytes"}:
            continue
        # open(p, "rb") / Path(p).open("rb") — binary mode, no encoding= needed
        if op_name == "open" and _has_binary_mode(node):
            continue
        if not _has_encoding_kwarg(node):
            findings.append({
                "file": posix,
                "line": node.lineno,
                "rule": "missing-encoding-kwarg",
                "evidence": f"{op_name}() called without encoding= kwarg",
            })

    # Check 3: module has print/echo but no stdout reconfigure
    # Only flags modules that actually do print/echo — pure-library modules
    # without any stdout writes don't need the reconfigure.
    if _has_print_like_call_anywhere(tree) and not _has_stdout_reconfigure(tree):
        findings.append({
            "file": posix,
            "line": 1,  # module-level finding
            "rule": "unconfigured-stdout",
            "evidence": (
                "Module contains print/click.echo calls but does not reconfigure "
                "sys.stdout to UTF-8. On Windows cp1252 stdout, any non-ASCII "
                "runtime data passed to print/echo will crash with "
                "UnicodeEncodeError. Add `sys.stdout.reconfigure(encoding=\"utf-8\", "
                "errors=\"replace\")` at module top."
            ),
        })

    return findings


def scan_path(root: Path, max_files: int = 500) -> list[dict]:
    """Walk a directory (or single file) and return findings from all .py files.

    Skips common build/cache directories. Bounded by max_files to avoid blowing
    up doctor latency on a 30+ project portfolio.
    """
    if root.is_file():
        if root.suffix == ".py":
            return check_file(root)
        return []

    skip_dirs = {
        ".venv", "venv", "__pycache__", ".pytest_cache", "build", "dist",
        ".tox", "node_modules", ".git", ".agent", ".project", "temp",
        "htmlcov", ".mypy_cache", "site-packages", ".eggs",
    }
    findings: list[dict] = []
    file_count = 0
    truncated = False
    for path in root.rglob("*.py"):
        # Codex PR#5 round-1 P1 fix: filter skip_dirs against parts RELATIVE
        # to root, not absolute path parts. Otherwise a project located
        # under an ancestor named `build`, `temp`, `dist`, etc. (e.g.
        # `C:/Users/build-bot/proj/...`) would be silently skipped.
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            # Defensive: rglob result should always be under root, but if a
            # filesystem oddity (symlink, junction) produces an outside path,
            # fall back to absolute parts rather than crash.
            rel_parts = path.parts
        if any(part in skip_dirs for part in rel_parts):
            continue
        file_count += 1
        if file_count > max_files:
            truncated = True
            break
        findings.extend(check_file(path))
    if truncated:
        # Surface (don't swallow): operator deserves to know their portfolio
        # outgrew the scan budget. Otherwise files past max_files vanish silently.
        findings.append({
            "file": root.as_posix(),
            "line": 0,
            "rule": "scan-truncated",
            "evidence": (
                f"scan stopped after {max_files} .py files; narrow scope or "
                "raise scan_path(max_files=) to see remaining files"
            ),
        })
    return findings
