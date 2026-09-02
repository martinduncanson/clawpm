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
  ``.reason``) on any malformation, INCLUDING frontmatter that parses to a
  non-mapping (reason ``"not_a_mapping"`` -- CLAWP-091: a hand-edited file
  whose frontmatter is a bare YAML scalar or list); returns ``(data, body)``
  on success, where ``data`` is always a ``dict``.

Both return ``body`` as the RAW remainder after the closing fence (the substring
the source files reconstruct from); callers ``.strip()`` / ``.lstrip()`` exactly
as they did before. ``parse_frontmatter``'s ``data`` is the raw
``yaml.safe_load(...) or {}`` result -- it is NOT coerced to ``dict``, so
lenient callers that assumed a mapping keep whatever downstream behaviour they
had for a non-mapping document (typically an ``AttributeError`` on the first
``.get()``, caught by their own broad ``except``). A caller that intends to
MUTATE frontmatter parsed via ``parse_frontmatter`` should run it through
:func:`require_mapping` first (CLAWP-091) -- see that function's docstring.

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
    """Raised by :func:`split_frontmatter` (and :func:`require_mapping`) when
    frontmatter is absent, malformed, or not a mapping.

    ``reason`` is one of:

    - ``"absent"`` -- the text has no leading ``---`` fence.
    - ``"unterminated"`` -- a leading fence with no closing fence.
    - ``"unparseable"`` -- a fenced block whose YAML failed to parse. The
      original :class:`yaml.YAMLError` is chained via ``__cause__``.
    - ``"not_a_mapping"`` -- the fenced block parsed successfully but to a
      non-mapping (e.g. a bare YAML scalar or list) -- CLAWP-091. Every
      mutation site needs a ``dict`` to assign/pop keys on; without this
      check a caller doing ``frontmatter[key] = value`` on a ``list`` or
      ``str`` raises a raw, unfriendly ``TypeError``.

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


def require_mapping(data: Any, *, where: str | None = None) -> dict[str, Any]:
    """Assert that parsed frontmatter is a mapping before a caller mutates it.

    ``parse_frontmatter`` is deliberately lenient and never raises, so a
    caller that intends to WRITE modified keys back (``data[key] = value``,
    ``data.pop(key)``, ...) after calling it must run the result through this
    guard first. Without it, a hand-edited file whose frontmatter is a bare
    YAML scalar or list (parses to a ``str``/``list``/``int``/etc. instead of
    a ``dict``) causes a raw, unfriendly ``TypeError`` at the first mutating
    assignment (CLAWP-091) instead of a clear, actionable error.

    ``split_frontmatter`` calls this internally, so its callers get the same
    guard automatically and don't need to call it themselves.

    Raises :class:`FrontmatterError` (reason ``"not_a_mapping"``) naming the
    file (via ``where``, when given) and the offending type if ``data`` is
    not a ``dict``. Returns ``data`` unchanged (narrowed to ``dict``)
    otherwise, so it composes as ``frontmatter = require_mapping(data)``.
    """
    if not isinstance(data, dict):
        location = f" in {where}" if where else ""
        raise FrontmatterError(
            "not_a_mapping",
            f"frontmatter{location} must be a YAML mapping (dict), got "
            f"{type(data).__name__} instead -- fix the file's frontmatter "
            "(between the '---' markers) before editing it.",
        )
    return data


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Strictly parse YAML frontmatter. Raises on any malformation.

    Returns ``(data, body)`` on success, where ``data`` is
    ``yaml.safe_load(parts[1]) or {}`` -- guaranteed to be a ``dict`` -- and
    ``body`` is the raw substring after the closing fence (NOT stripped).

    Raises :class:`FrontmatterError` with ``reason``:

    - ``"absent"`` if ``text`` has no leading ``---`` fence,
    - ``"unterminated"`` if the fence is never closed,
    - ``"unparseable"`` if the fenced YAML fails to parse (chaining the
      original :class:`yaml.YAMLError`),
    - ``"not_a_mapping"`` if the fenced YAML parses but to a non-mapping
      (CLAWP-091; see :func:`require_mapping`).
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
    return require_mapping(data), parts[2]
