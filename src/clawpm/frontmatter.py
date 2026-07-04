"""Canonical YAML-frontmatter parsing for clawpm task/research/mission files.

Historically the ``text.split("---", 2)`` + ``yaml.safe_load(parts[1]) or {}``
dance was hand-rolled in ~14 sites across 8 modules, each with subtly different
malformed-input handling (CLAWP-079). This module centralises the *parse*;
callers keep their own malformed-input *policy* (lenient read, skip, raise,
synthesize) because those policies are deliberately divergent and, in several
cases, review-shaped (CLAWP-066/067).

Two entry points:

- :func:`parse_frontmatter` -- lenient. Never raises. Returns ``(data, body)``.
- :func:`split_frontmatter` -- strict. Raises :class:`FrontmatterError` (with a
  ``.reason``) on any malformation; returns ``(data, body)`` on success.

Both return ``body`` as the RAW remainder after the closing fence (the substring
the source files reconstruct from); callers ``.strip()`` / ``.lstrip()`` exactly
as they did before. ``data`` is the raw ``yaml.safe_load(...) or {}`` result --
it is NOT coerced to ``dict``, so callers that assumed a mapping keep whatever
downstream behaviour they had for a non-mapping document.

Serialization is intentionally NOT centralised: the rewrite sites emit with
divergent ``yaml.dump`` options (``sort_keys``, ``safe_dump`` vs ``dump``,
``.strip()`` vs ``.rstrip()``, body ``.lstrip("\\n")``), and unifying them would
change on-disk bytes.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import yaml

_FENCE = "---"


def stamp_updated(frontmatter: dict[str, Any], when: str | None = None) -> None:
    """Set the ``updated`` timestamp on a frontmatter mapping, in place (CLAWP-086).

    Single source of the field name + ISO-date format so every task mutator
    stamps identically. ``when`` defaults to today's ISO date; a caller that
    also stamps ``created`` in the same write passes the shared value so
    ``created == updated`` holds on creation.
    """
    frontmatter["updated"] = when or date.today().isoformat()


class FrontmatterError(ValueError):
    """Raised by :func:`split_frontmatter` when frontmatter is absent or malformed.

    ``reason`` is one of:

    - ``"absent"`` -- the text has no leading ``---`` fence.
    - ``"unterminated"`` -- a leading fence with no closing fence.
    - ``"unparseable"`` -- a fenced block whose YAML failed to parse. The
      original :class:`yaml.YAMLError` is chained via ``__cause__``.

    It subclasses :class:`ValueError` so existing ``except ValueError`` /
    ``except (ValueError, ...)`` handlers keep catching it.
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


def parse_frontmatter(text: str) -> tuple[Any, str]:
    """Leniently parse YAML frontmatter. Never raises on ``str`` input.

    YAML parse errors are swallowed (see below); only grossly invalid,
    non-``str`` input could raise (e.g. ``AttributeError`` from ``.startswith``),
    which every caller avoids by passing file text.

    Returns ``(data, body)``:

    - Fenced block with parseable YAML -> ``(yaml.safe_load(...) or {},
      remainder_after_closing_fence)``.
    - Fenced block whose YAML is unparseable -> ``({}, remainder_after_closing_fence)``
      -- the malformed YAML is dropped but the body is preserved, so a rewrite
      caller does not rebuild a double-frontmatter file.
    - No leading fence, or an unterminated fence -> ``({}, text)``.

    ``body`` is the raw substring after the closing fence -- NOT stripped.
    """
    if text.startswith(_FENCE):
        parts = text.split(_FENCE, 2)
        if len(parts) >= 3:
            try:
                data = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                return {}, parts[2]
            return data, parts[2]
    return {}, text


def split_frontmatter(text: str) -> tuple[Any, str]:
    """Strictly parse YAML frontmatter. Raises on any malformation.

    Returns ``(data, body)`` on success, where ``data`` is
    ``yaml.safe_load(parts[1]) or {}`` and ``body`` is the raw substring after
    the closing fence (NOT stripped).

    Raises :class:`FrontmatterError` with ``reason``:

    - ``"absent"`` if ``text`` has no leading ``---`` fence,
    - ``"unterminated"`` if the fence is never closed,
    - ``"unparseable"`` if the fenced YAML fails to parse (chaining the
      original :class:`yaml.YAMLError`).
    """
    if not text.startswith(_FENCE):
        raise FrontmatterError("absent", "no frontmatter fence")
    parts = text.split(_FENCE, 2)
    if len(parts) < 3:
        raise FrontmatterError("unterminated", "unterminated frontmatter fence")
    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise FrontmatterError("unparseable", f"unparseable frontmatter: {exc}") from exc
    return data, parts[2]
