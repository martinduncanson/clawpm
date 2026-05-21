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

PRINT_LIKE_ATTRS = {"echo", "secho", "print"}
"""Attribute names that act as stdout-writers (covers click.echo, click.secho,
typer.echo, sys.stdout.print, etc.)."""

FILE_OP_ATTRS = {"read_text", "write_text", "open", "read_bytes", "write_bytes"}
"""Pathlib methods that take an encoding= kwarg (or bypass it for bytes)."""


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
    """Return True if this Call is a print() or click.echo()-shaped call."""
    func = node.func
    if isinstance(func, ast.Name) and func.id in PRINT_LIKE_NAMES:
        return True
    if isinstance(func, ast.Attribute) and func.attr in PRINT_LIKE_ATTRS:
        return True
    return False


def _is_file_op_call(node: ast.Call) -> tuple[bool, str]:
    """Return (is_file_op, name) for the call.

    Detects: `open(...)`, `<path>.open(...)`, `<path>.read_text(...)`,
    `<path>.write_text(...)`. The bytes variants are tracked but reported
    separately because they don't take encoding=.
    """
    func = node.func
    if isinstance(func, ast.Name) and func.id == "open":
        return True, "open"
    if isinstance(func, ast.Attribute) and func.attr in FILE_OP_ATTRS:
        return True, func.attr
    return False, ""


def _has_encoding_kwarg(node: ast.Call) -> bool:
    """Return True if any keyword arg is named 'encoding'."""
    return any(kw.arg == "encoding" for kw in node.keywords)


def _has_stdout_reconfigure(tree: ast.Module) -> bool:
    """Return True if the module's top-level statements include a
    sys.stdout.reconfigure(...) call.

    Conservative: only looks at module-level statements (not nested in
    conditionals, functions, classes). Anything more nuanced is a guard
    the operator has made deliberately.
    """
    for stmt in tree.body:
        # Match `sys.stdout.reconfigure(...)` or
        # `if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(...)`
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            if (isinstance(call.func, ast.Attribute)
                    and call.func.attr == "reconfigure"
                    and isinstance(call.func.value, ast.Attribute)
                    and call.func.value.attr == "stdout"):
                return True
        # Allow the common `if hasattr(...): reconfigure(...)` guard
        if isinstance(stmt, ast.If):
            for body_stmt in stmt.body:
                if isinstance(body_stmt, ast.Expr) and isinstance(body_stmt.value, ast.Call):
                    call = body_stmt.value
                    if (isinstance(call.func, ast.Attribute)
                            and call.func.attr == "reconfigure"
                            and isinstance(call.func.value, ast.Attribute)
                            and call.func.value.attr == "stdout"):
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
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return findings
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        # File doesn't parse — skip, don't crash doctor
        return findings

    posix = file_path.as_posix()

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
        "htmlcov", ".mypy_cache",
    }
    findings: list[dict] = []
    file_count = 0
    for path in root.rglob("*.py"):
        if any(part in skip_dirs for part in path.parts):
            continue
        file_count += 1
        if file_count > max_files:
            break
        findings.extend(check_file(path))
    return findings
