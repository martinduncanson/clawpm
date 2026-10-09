"""CLAWP-102 -- doc-staleness gate: README "All commands" vs ``clawpm introspect``.

``clawpm introspect`` (CLAWP-088) is the ground truth for what is shipped.
Every non-hidden command path and every long option it exposes must be
mentioned somewhere in the README's mechanical reference section
(``## All commands``). Narrative sections are deliberately not gated.

Matching is intentionally coarse (presence, not per-command flag placement):
it catches "shipped but undocumented" without forcing a README rewrite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from clawpm.cli import main
from clawpm.introspect import build_introspection

README = Path(__file__).resolve().parent.parent / "README.md"
SECTION_HEADING = "## All commands"
_FLAG_RE = re.compile(r"(?<![\w-])--[a-z0-9][a-z0-9-]*")


def extract_all_commands_section(readme_text: str) -> str:
    """Return the body of ``## All commands`` up to the next ``## `` heading."""
    lines = readme_text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == SECTION_HEADING)
    except StopIteration:
        raise AssertionError(f"README has no '{SECTION_HEADING}' section") from None
    body: list[str] = []
    in_fence = False
    for ln in lines[start + 1 :]:
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and ln.startswith("## "):
            break
        body.append(ln)
    return "\n".join(body)


def _walk(cmd: dict, path: tuple[str, ...]):
    for name, sub in (cmd.get("commands") or {}).items():
        if sub.get("hidden"):
            continue
        sub_path = (*path, name)
        yield sub_path, sub
        yield from _walk(sub, sub_path)


def shipped_surface(doc: dict) -> tuple[set[tuple[str, ...]], set[str]]:
    """(command paths, long options) of every non-hidden command in ``doc``."""
    root = doc["command"]
    paths: set[tuple[str, ...]] = set()
    flags: set[str] = set()

    def add_flags(cmd: dict) -> None:
        for p in cmd.get("params", []):
            if p.get("hidden"):
                continue
            for opt in (*p.get("opts", []), *p.get("secondary_opts", [])):
                if opt.startswith("--"):
                    flags.add(opt)

    add_flags(root)
    for path, cmd in _walk(root, ()):
        paths.add(path)
        add_flags(cmd)
    return paths, flags


def find_gaps(doc: dict, section: str) -> tuple[list[str], list[str]]:
    """Return (missing command paths, missing long options) vs ``section``."""
    paths, flags = shipped_surface(doc)
    missing_cmds = []
    for path in sorted(paths):
        pattern = r"(?<![\w-])clawpm\s+" + r"\s+".join(map(re.escape, path)) + r"(?![\w-])"
        if not re.search(pattern, section):
            missing_cmds.append("clawpm " + " ".join(path))
    mentioned = set(_FLAG_RE.findall(section))
    return missing_cmds, sorted(flags - mentioned)


@pytest.fixture(scope="module")
def doc() -> dict:
    return build_introspection(main)


def test_readme_all_commands_covers_introspect(doc):
    section = extract_all_commands_section(README.read_text(encoding="utf-8"))
    cmds, flags = find_gaps(doc, section)
    assert not cmds and not flags, (
        "README '## All commands' is stale vs `clawpm introspect`.\n"
        f"  commands with no mention: {cmds}\n"
        f"  options with no mention: {flags}\n"
        "Add them to the reference tables/blocks (CLAWP-102)."
    )


# --- red tests: the gate must actually fail on a stale README -------------


def _fake_doc() -> dict:
    def opt(*opts):
        return {"opts": list(opts), "secondary_opts": [], "hidden": False}

    return {
        "command": {
            "params": [opt("--format")],
            "commands": {
                "frob": {"hidden": False, "params": [opt("--zap")], "commands": {}},
                "ghost": {"hidden": True, "params": [opt("--secret")], "commands": {}},
                "grp": {
                    "hidden": False,
                    "params": [],
                    "commands": {"sub": {"hidden": False, "params": [], "commands": {}}},
                },
            },
        }
    }


def test_gate_fails_on_synthetic_stale_readme():
    readme = "# T\n\n## All commands\n\n```bash\nclawpm grp\n```\n\n## Next\nclawpm frob --zap --format\n"
    cmds, flags = find_gaps(_fake_doc(), extract_all_commands_section(readme))
    # frob/--zap/--format only appear OUTSIDE the section; grp sub is missing.
    assert "clawpm frob" in cmds
    assert "clawpm grp sub" in cmds
    assert "--zap" in flags and "--format" in flags
    assert "clawpm ghost" not in cmds and "--secret" not in flags  # hidden skipped


def test_gate_passes_on_synthetic_complete_readme():
    readme = (
        "## All commands\n\n| `clawpm frob --zap` | x |\n```bash\nclawpm grp sub  # --format\n```\n\n## Next\n"
    )
    assert find_gaps(_fake_doc(), extract_all_commands_section(readme)) == ([], [])


def test_prefix_does_not_satisfy_longer_command_or_flag():
    readme = "## All commands\nclawpm frobnicate --zapper --format\nclawpm grp\n"
    cmds, flags = find_gaps(_fake_doc(), extract_all_commands_section(readme))
    assert "clawpm frob" in cmds
    assert "--zap" in flags
