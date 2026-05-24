"""Static analysis for Windows cp1252-stdout / file-encoding risks (CLAWP-011).

See feedback-windows-cp1252-write-text memory for case history (7 confirmed
crashes between 2026-05-08 and 2026-05-18). This module scans .py files for
two patterns that reliably crash on Windows default cp1252 stdout / file I/O:

  1. Non-ASCII glyphs in print() / *.echo() string literals (UnicodeEncodeError
     when stdout is cp1252-backed, the Windows default).
  2. open() / Path.read_text() / Path.write_text() / Path.open() calls without
     an explicit encoding= kwarg (default cp1252 mojibake on UTF-8 files).

Uses AST-based detection. Regex was the alternative considered in
predictions.unknowns; AST is preferred because:
  - f-strings (JoinedStr) have heterogeneous components — regex on the raw
    source line would need to ignore non-Constant pieces.
  - encoding= kwarg detection is unambiguous on Call nodes; brittle by regex.
  - Multi-line string literals are handled natively.

The check itself MUST be cp1252-clean per the task's pre_mortem
(`self-test against its own source file before declaring done`). All
docstrings and strings here are ASCII; no shell-pipe glyphs, no smart quotes.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Directories never worth scanning: third-party, build artefacts, VCS internals.
# Matched against any path component (Path.parts) of the candidate file.
SKIP_DIR_PARTS = frozenset({
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "build",
    "dist",
    ".git",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
    ".eggs",
})

FILE_OPS = frozenset({"open", "read_text", "write_text"})
"""Call names that default to cp1252 on Windows when encoding= is omitted.

`Path.open()` is included as an attribute lookup because `pathlib.Path.open`
inherits Python's text-mode encoding default. Bare `open(...)` (builtin) and
explicit `*.open()` calls both match.
"""


@dataclass(frozen=True)
class EncodingFinding:
    """A single cp1252-risk hit.

    kind is one of:
      - "non_ascii_print"      — non-ASCII char in print()/*.echo() literal
      - "file_op_no_encoding"  — open/read_text/write_text without encoding=
    """

    path: Path
    line: int
    kind: str
    snippet: str
    detail: str


def _is_print_or_echo(node: ast.Call) -> bool:
    """Match print(...) and any *.echo(...) call.

    Matching *.echo (not strictly click.echo) is deliberate breadth:
      - click.echo is the canonical hit
      - typer.echo wraps it
      - imports like `from click import echo` make `echo(...)` a Name not Attr,
        but the Name path is handled separately
      - any future *.echo wrapper a project might add stays covered
    The false-positive cost is low (rare to call something else `.echo()`
    with non-ASCII literals in a Python source file).
    """
    f = node.func
    if isinstance(f, ast.Name) and f.id in ("print", "echo"):
        return True
    if isinstance(f, ast.Attribute) and f.attr == "echo":
        return True
    return False


def _non_ascii_in_string_args(node: ast.Call) -> tuple[bool, str]:
    """Inspect every positional string-literal arg for non-ASCII content.

    Handles two arg shapes:
      - ast.Constant with str value (plain "..." literal)
      - ast.JoinedStr (f-string) — iterate its Constant pieces

    Skipped intentionally:
      - Name args (variables) — content unknowable statically
      - BinOp string concatenation — would need constant-folding to be sound
      - Bytes literals — cp1252 risk is on str-to-stdout, not bytes
    """
    offenders: list[str] = []
    kind_label = "literal"
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if not arg.value.isascii():
                offenders.extend(c for c in arg.value if ord(c) > 127)
        elif isinstance(arg, ast.JoinedStr):
            for piece in arg.values:
                if (
                    isinstance(piece, ast.Constant)
                    and isinstance(piece.value, str)
                    and not piece.value.isascii()
                ):
                    offenders.extend(c for c in piece.value if ord(c) > 127)
                    kind_label = "f-string literal"
    if not offenders:
        return False, ""
    # Cap the detail string so the doctor output stays one line per finding
    # even when a single literal contains many offenders.
    sample = "".join(offenders[:3])
    suffix = "..." if len(offenders) > 3 else ""
    return True, f"{kind_label} contains: {sample}{suffix}"


def _file_op_name(node: ast.Call) -> str | None:
    """Return the file-op name if this Call is open/read_text/write_text.

    Matches both call shapes:
      - bare `open(...)` (Name node, builtin or shadowed)
      - `something.read_text(...)`, `something.write_text(...)`, `something.open(...)`
        (Attribute node — any receiver, since static type inference is out of scope)

    The `something.open(...)` match is broader than strictly Path.open — any
    receiver's `.open()` triggers. The encoding= heuristic is still right for
    io.open, tempfile.NamedTemporaryFile().open chains, etc.; in the rare case
    a `.open()` call legitimately can't take encoding= (e.g. socket.open), the
    operator inspects the finding and ignores it. False-positive cost stays low.
    """
    f = node.func
    if isinstance(f, ast.Name) and f.id == "open":
        return "open"
    if isinstance(f, ast.Attribute) and f.attr in FILE_OPS:
        return f.attr
    return None


def _has_encoding_kwarg(node: ast.Call) -> bool:
    """True if any keyword arg is named encoding= or is **kwargs (unknown).

    `**kwargs` is treated as "encoding may be present" — a deliberate
    false-negative to avoid noise on wrapper functions that forward kwargs.
    Concerns block of the PR calls this out.
    """
    for kw in node.keywords:
        # kw.arg is None for **kwargs unpacking; treat as "could contain encoding".
        if kw.arg is None or kw.arg == "encoding":
            return True
    return False


def _is_binary_mode(node: ast.Call) -> bool:
    """True if the call has a `mode=` argument (kw or 2nd positional) containing 'b'.

    Binary mode means encoding= is illegal — flagging it would be a false positive.
    Handles:
      - open(p, "rb")                       — Constant str positional
      - open(p, mode="rb")                  — keyword Constant
      - p.read_bytes() / p.write_bytes()    — different method names, not in FILE_OPS
    """
    # Positional mode arg: for open(p, mode) the 2nd positional is mode.
    if len(node.args) >= 2:
        mode_arg = node.args[1]
        if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
            if "b" in mode_arg.value:
                return True
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            if "b" in kw.value.value:
                return True
    return False


def scan_file(path: Path) -> list[EncodingFinding]:
    """Scan a single .py file. Unreadable / unparseable files return [].

    Bytes-then-decode (errors='replace') is deliberate: a syntactically-valid
    file with stray non-UTF-8 bytes still parses for our purposes, and surfacing
    a SyntaxError from this check would defeat its purpose (the operator runs
    doctor to find risks, not to learn that one of their files won't decode).
    """
    findings: list[EncodingFinding] = []
    try:
        raw = path.read_bytes()
    except OSError:
        return findings
    source = raw.decode("utf-8", errors="replace")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return findings

    source_lines = source.splitlines()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        line = getattr(node, "lineno", 0)
        snippet = ""
        if 1 <= line <= len(source_lines):
            snippet = source_lines[line - 1].strip()[:120]

        if _is_print_or_echo(node):
            hit, detail = _non_ascii_in_string_args(node)
            if hit:
                findings.append(
                    EncodingFinding(
                        path=path,
                        line=line,
                        kind="non_ascii_print",
                        snippet=snippet,
                        detail=detail,
                    )
                )

        op = _file_op_name(node)
        if op is not None and not _has_encoding_kwarg(node) and not _is_binary_mode(node):
            findings.append(
                EncodingFinding(
                    path=path,
                    line=line,
                    kind="file_op_no_encoding",
                    snippet=snippet,
                    detail=f"{op}() missing encoding=",
                )
            )

    return findings


def scan_paths(paths: Iterable[Path]) -> list[EncodingFinding]:
    """Scan every .py file under each root, skipping vendor / build dirs.

    A root may be either a file (scanned directly if .py) or a directory
    (recursively globbed for *.py). Order is preserved across roots but not
    within rglob — pytest assertions should sort by (path, line) if they
    care about ordering.
    """
    findings: list[EncodingFinding] = []
    for root in paths:
        if not isinstance(root, Path):
            root = Path(root)
        if root.is_file():
            if root.suffix == ".py":
                findings.extend(scan_file(root))
            continue
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            if any(part in SKIP_DIR_PARTS for part in py.parts):
                continue
            findings.extend(scan_file(py))
    return findings


def format_finding(f: EncodingFinding) -> str:
    """One-line doctor output. Matches the [WARNING] [scope] style of other checks."""
    return (
        f"[WARNING] [cp1252-risk] {f.path.as_posix()}:{f.line} "
        f"[{f.kind}] {f.detail} | {f.snippet}"
    )
