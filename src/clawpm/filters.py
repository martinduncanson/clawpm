"""Composable task filters (CLAWP-069).

A ``TaskFilter`` is any predicate ``Task -> bool``. ``apply_filters`` runs a
task list through a set of filters combined with AND (a task must satisfy every
filter to survive). This keeps the filtering surface open for extension: new
axes (text search, priority, complexity, parent, limit — CLAWP-082) add their
own ``by_*`` constructor and drop straight into the same ``apply_filters`` call
with no change to callers.

Tags are the first axis. Repeated ``--tag`` is OR by default (a task matching
ANY requested tag survives); ``match_all=True`` switches to AND (a task must
carry EVERY requested tag).
"""

from __future__ import annotations

import operator
import re
from collections.abc import Callable, Iterable, Sequence

from .models import Task, normalize_tags

TaskFilter = Callable[[Task], bool]


def by_tags(tags: Sequence[str], match_all: bool = False) -> TaskFilter:
    """Filter tasks by workstream tag.

    ``tags`` is normalised the same way stored tags are (lowercased, deduped),
    so ``--tag Concurrency`` matches a task tagged ``concurrency``. With
    ``match_all=False`` (default) a task survives if it carries any requested
    tag (OR); with ``match_all=True`` it must carry all of them (AND).

    An empty/blank ``tags`` set yields a filter that matches nothing — asking to
    filter by no tag is treated as an unsatisfiable query rather than a no-op,
    so a typo'd flag doesn't silently return the whole backlog.
    """
    wanted = normalize_tags(list(tags))

    def _pred(task: Task) -> bool:
        if not wanted:
            return False
        # Defensive: the predicate assumes the post-normalise invariant (tags
        # are hashable strings), but a Task built outside the load path could
        # carry non-strings — filter to str so a stray value can't raise here.
        have = {t for t in task.tags if isinstance(t, str)}
        if match_all:
            return all(w in have for w in wanted)
        return any(w in have for w in wanted)

    return _pred


def _haystack(task: Task) -> str:
    """The text a ``--text`` query searches: title + body."""
    return f"{task.title}\n{task.content}"


def by_text(query: str, use_regex: bool = False) -> TaskFilter:
    """Filter tasks by a text query over title + body (CLAWP-082).

    ``use_regex=False`` (default) is a case-insensitive substring match;
    ``use_regex=True`` compiles ``query`` as a case-insensitive regular
    expression. A blank query — or a regex that fails to compile — yields a
    filter that matches nothing, so a typo can't silently return the whole
    backlog (mirrors ``by_tags``' unsatisfiable-empty behaviour).
    """
    if not query or not query.strip():
        return lambda _t: False
    if use_regex:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            return lambda _t: False
        return lambda t: pattern.search(_haystack(t)) is not None
    needle = query.lower()
    return lambda t: needle in _haystack(t).lower()


_PRIORITY_COMPARATORS: list[tuple[str, Callable[[int, int], bool]]] = [
    ("<=", operator.le),
    (">=", operator.ge),
    ("==", operator.eq),
    ("<", operator.lt),
    (">", operator.gt),
    ("=", operator.eq),
]


def by_priority(spec: str | int) -> TaskFilter:
    """Filter tasks by priority (CLAWP-082).

    ``spec`` is an exact value (``5``) or a comparator expression
    (``<=3``, ``>7``, ``>=2``) — priority is ordinal (lower = more urgent),
    so ranges are the useful query. An unparseable spec matches nothing.
    """
    s = str(spec).strip()
    op = operator.eq
    num = s
    for prefix, fn in _PRIORITY_COMPARATORS:
        if s.startswith(prefix):
            op = fn
            num = s[len(prefix):].strip()
            break
    try:
        value = int(num)
    except (TypeError, ValueError):
        return lambda _t: False
    return lambda t: op(t.priority, value)


def by_complexity(values: Sequence[str]) -> TaskFilter:
    """Filter tasks by complexity (CLAWP-082), OR across the requested values.

    Values are lowercased (``s``/``m``/``l``/``xl``). A task with no complexity
    set never matches. An empty request matches nothing.
    """
    wanted = {v.strip().lower() for v in values if isinstance(v, str) and v.strip()}

    def _pred(task: Task) -> bool:
        if not wanted:
            return False
        current = task.complexity.value if task.complexity else None
        return current in wanted

    return _pred


def by_parent(parent_id: str) -> TaskFilter:
    """Filter to the direct subtasks of ``parent_id`` (CLAWP-082).

    A blank parent id matches nothing (asking for children of nothing is an
    unsatisfiable query, not "all root tasks").
    """
    pid = (parent_id or "").strip()
    if not pid:
        return lambda _t: False
    return lambda t: (t.parent or "") == pid


def by_linked(referencing_ids: Iterable[str]) -> TaskFilter:
    """Filter to tasks whose id is in ``referencing_ids`` (CLAWP-082).

    The caller precomputes the referencing-id set from the derived link index
    (``links.build_link_index(...).referencing_ids(target)``) — every entity
    that references the target via a ``[[id]]`` wiki-link OR a typed edge. This
    keeps ``filters`` decoupled from the link-graph machinery (models-only
    import surface). An empty set matches nothing.
    """
    wanted = {i for i in referencing_ids if isinstance(i, str)}
    return lambda t: t.id in wanted


def apply_filters(tasks: Iterable[Task], filters: Iterable[TaskFilter]) -> list[Task]:
    """Return the tasks satisfying every filter (AND across filters).

    ``filters`` may be empty, in which case the tasks pass through unchanged.
    """
    filter_list = [f for f in filters if f is not None]
    if not filter_list:
        return list(tasks)
    return [t for t in tasks if all(f(t) for f in filter_list)]
