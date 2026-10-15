"""Surgical top-level frontmatter edits (CLAWP-154).

Setting or removing a handful of top-level keys must not reserialise the
whole mapping: that loses comments, key order, quoting and scalar style on
every routine write. :func:`splice_keys` replaces only the lines of the named
keys and re-parses before returning, so a splice that would change any other
key raises instead of reaching disk. It reuses the event-mark technique (and
the parsing helpers) of ``pr_record`` (CLAWP-151): refusing duplicate, aliased,
flow-style or indented keys, and block scalars, rather than guessing. Anchors,
aliases and tags elsewhere in the mapping are left alone; a replaced value may
carry them unless another key aliases an anchor it defines.
"""

from __future__ import annotations

import copy
from typing import Any

import yaml

from clawpm.pr_record import (
    PrRecordError,
    _BOM,
    _ODD_BREAKS,
    _top_level_pairs,
    _value_extent,
)


class _Remove:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "REMOVE"


REMOVE = _Remove()


def _render(key: str, value: Any) -> list[str]:
    """Block-style YAML lines for ``{key: value}`` (no trailing newline)."""
    text = yaml.safe_dump(
        {key: value}, default_flow_style=False, allow_unicode=True,
        sort_keys=False, width=4096,
    )
    return text.rstrip("\n").split("\n")


def _frontmatter_bounds(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        raise PrRecordError("no_frontmatter", "task file has no leading '---' frontmatter fence")
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close is None:
        raise PrRecordError("no_frontmatter", "task file frontmatter fence is not terminated")
    return close


def _fm_text(lines: list[str], close: int) -> str:
    return "\n".join(ln.rstrip("\r") for ln in lines[1:close])


def _orphaned_anchor(key: str, value_events: list[Any], pairs: list[Any]) -> str | None:
    """An anchor defined inside ``key``'s value that another top-level value aliases.

    Replacing or removing ``key`` would leave that alias dangling. Conservative:
    any alias of the same name counts, whatever its position.
    """
    defined = {e.anchor for e in value_events if getattr(e, "anchor", None)}
    if not defined:
        return None
    for other, events in pairs:
        if other.value == key:
            continue
        for ev in events:
            if isinstance(ev, yaml.AliasEvent) and ev.anchor in defined:
                return ev.anchor
    return None


def _plain_events(value_events: list[Any]) -> list[Any]:
    """``value_events`` with anchors, tags and aliases neutralised.

    Only the key being replaced is passed through ``_value_extent``, whose
    anchor/alias/tag refusal is for callers that cannot replace such a value.
    Here the whole value is rewritten, so those markers are safe once no other
    key aliases an anchor defined inside it (checked by :func:`_orphaned_anchor`).
    Block scalars and flow layouts are still judged by ``_value_extent``.
    """
    out: list[Any] = []
    for ev in value_events:
        if isinstance(ev, yaml.AliasEvent):
            out.append(yaml.ScalarEvent(
                None, None, (True, False), "", ev.start_mark, ev.end_mark, None
            ))
        elif getattr(ev, "anchor", None) or getattr(ev, "tag", None):
            clone = copy.copy(ev)
            clone.anchor = None
            clone.tag = None
            out.append(clone)
        else:
            out.append(ev)
    return out


def _splice_one(lines: list[str], key: str, value: Any) -> list[str]:
    close = _frontmatter_bounds(lines)
    fm_lines = [ln.rstrip("\r") for ln in lines[1:close]]
    fm_text = "\n".join(fm_lines)
    pairs = _top_level_pairs(fm_text)
    eol = "\r" if lines[0].endswith("\r") else ""
    existing = next(((k, v) for k, v in pairs if k.value == key), None)
    out = list(lines)
    if existing is None:
        if value is REMOVE:
            return out
        new = [ln + eol for ln in _render(key, value)]
        out[close:close] = new
        return out
    orphan = _orphaned_anchor(key, existing[1], pairs)
    if orphan is not None:
        raise PrRecordError(
            "bad_frontmatter",
            f"'{key}' defines anchor '&{orphan}' that another key aliases; refusing to replace it",
        )
    first, last, tail = _value_extent(
        existing[0], _plain_events(existing[1]), fm_text, fm_lines, key
    )
    if value is REMOVE:
        out[1 + first:1 + last + 1] = []
        return out
    new = [ln + eol for ln in _render(key, value)]
    new[-1] = new[-1][: len(new[-1]) - len(eol)] + tail + eol if eol else new[-1] + tail
    out[1 + first:1 + last + 1] = new
    return out


def splice_keys(text: str, updates: dict[str, Any]) -> str:
    """Return ``text`` with only the top-level ``updates`` keys changed.

    ``updates`` maps a key to its new value, or to :data:`REMOVE`. The BOM,
    line endings (LF or CRLF) and every other byte are preserved. Raises
    ``PrRecordError`` (``bad_frontmatter`` / ``no_frontmatter`` /
    ``verify_failed``) when the frontmatter cannot be spliced safely or the
    result does not read back as expected; nothing is written by this function.
    """
    bom = _BOM if text.startswith(_BOM) else ""
    body = text[len(bom):]
    lines = body.split("\n")
    close = _frontmatter_bounds(lines)
    original_fm = _fm_text(lines, close)
    if any(ch in original_fm for ch in _ODD_BREAKS):
        raise PrRecordError(
            "bad_frontmatter", "task frontmatter contains a line separator other than LF/CRLF"
        )
    try:
        parsed = yaml.safe_load(original_fm)
    except yaml.YAMLError as exc:
        raise PrRecordError("bad_frontmatter", f"task frontmatter is not parseable YAML: {exc}")
    if not isinstance(parsed, dict):
        raise PrRecordError("bad_frontmatter", "task frontmatter is not a mapping")

    for key, value in updates.items():
        lines = _splice_one(lines, key, value)
    out = "\n".join(lines)

    # Re-parse before anything is committed: every updated key reads back as
    # intended, and no OTHER top-level key changed.
    new_lines = out.split("\n")
    try:
        new_close = _frontmatter_bounds(new_lines)
        new_text = _fm_text(new_lines, new_close)
        check = yaml.safe_load(new_text)
        _top_level_pairs(new_text)  # no duplicate / aliased keys introduced
    except (yaml.YAMLError, PrRecordError) as exc:
        raise PrRecordError("verify_failed", f"spliced frontmatter does not parse cleanly: {exc}")
    if not isinstance(check, dict):
        raise PrRecordError("verify_failed", "spliced frontmatter is not a mapping")
    for key, value in updates.items():
        if value is REMOVE:
            if key in check:
                raise PrRecordError("verify_failed", f"'{key}' was not removed")
        elif check.get(key) != yaml.safe_load(yaml.safe_dump(value, allow_unicode=True)):
            raise PrRecordError("verify_failed", f"spliced '{key}' did not read back as written")
    others = {k for k in set(parsed) | set(check) if k not in updates}
    if any(parsed.get(k) != check.get(k) or (k in parsed) != (k in check) for k in others):
        raise PrRecordError("verify_failed", "splice changed a frontmatter key it was not asked to change")
    return bom + out
