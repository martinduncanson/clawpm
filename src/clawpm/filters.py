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


def apply_filters(tasks: Iterable[Task], filters: Iterable[TaskFilter]) -> list[Task]:
    """Return the tasks satisfying every filter (AND across filters).

    ``filters`` may be empty, in which case the tasks pass through unchanged.
    """
    filter_list = [f for f in filters if f is not None]
    if not filter_list:
        return list(tasks)
    return [t for t in tasks if all(f(t) for f in filter_list)]
