"""Freeform wiki-link graph + derived backlinks (CLAWP-082).

clawpm already has a TYPED graph — ``depends``, ``parent``/``children``,
``reference_tasks``, ``supersedes``, ``parent_mission``, and mission
``mini_goals``. This module layers the freeform ``[[id]]`` wiki-link convention
on top and, crucially, DERIVES *backlinks*: an agent opening ``CLAWP-042`` can
now see every task/research/mission that references it — whether via a
wiki-link or a typed edge (the two graphs are unified in ``linked_from``).

Doctrine (CLAWP-061): the readable markdown files stay authoritative. This
index is computed at read time from those files and is fully rebuildable — no
graph DB, no persisted cache, no visualisation layer. The portfolio is small
enough that a per-query scan is cheap; a cache is deliberately deferred until
profiling says it is needed (CLAWP-082 pre-mortem).

The wiki-link parser itself lives in ``models.extract_wiki_links`` so the
per-entity load paths can populate their own outbound ``links`` without an
import cycle back through this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import PortfolioConfig, TaskState, extract_wiki_links

# ``via`` labels name the edge as authored on the REFERENCING entity, so a
# backlink reads "referenced by <id> via <field>". "wiki" is the freeform
# ``[[id]]`` edge; the rest mirror the typed-graph frontmatter fields.
VIA_WIKI = "wiki"


@dataclass
class LinkIndex:
    """A read-time snapshot of the unified link graph for one project.

    ``outbound[id]`` / ``inbound[id]`` are ordered ``(other_id, via)`` lists
    (de-duplicated). ``ids`` is the set of known entity ids (used for dangling
    detection); ``kinds`` maps each id to ``"task"``/``"research"``/``"mission"``.
    """

    ids: set[str] = field(default_factory=set)
    kinds: dict[str, str] = field(default_factory=dict)
    outbound: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    inbound: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def _register(self, src: str, dst: str, via: str) -> None:
        if not dst or dst == src:
            # Drop self-references — a task linking to itself is noise, not a
            # backlink worth surfacing.
            return
        ob = self.outbound.setdefault(src, [])
        if (dst, via) not in ob:
            ob.append((dst, via))
        ib = self.inbound.setdefault(dst, [])
        if (src, via) not in ib:
            ib.append((src, via))

    def links_of(self, entity_id: str) -> list[str]:
        """Outbound freeform wiki-link targets authored in this entity."""
        return [dst for dst, via in self.outbound.get(entity_id, []) if via == VIA_WIKI]

    def linked_from(self, entity_id: str) -> list[dict[str, str]]:
        """Backlinks: every entity referencing ``entity_id`` (wiki + typed).

        Sorted by ``(id, via)`` for deterministic output.
        """
        rows = self.inbound.get(entity_id, [])
        return [
            {"id": rid, "via": via}
            for rid, via in sorted(set(rows), key=lambda rv: (rv[0], rv[1]))
        ]

    def referencing_ids(self, entity_id: str) -> set[str]:
        """The bare set of entity ids that reference ``entity_id`` (any edge)."""
        return {rid for rid, _ in self.inbound.get(entity_id, [])}


def build_link_index(config: PortfolioConfig, project_id: str) -> LinkIndex:
    """Scan a project's tasks + research + missions into a :class:`LinkIndex`.

    Tasks are scanned across every state (open/progress/blocked/done and the
    rejected won't-do ledger) so a backlink from a completed or rejected task is
    not lost. Both freeform ``[[id]]`` links and the typed edges are registered,
    unifying the two graphs in ``inbound``/``linked_from``.
    """
    # Local imports: tasks/research/mission import models but not links, so
    # importing them here (not at module top) keeps the dependency graph acyclic
    # and avoids paying the cost when only the pure parser is needed.
    from .tasks import list_tasks
    from .research import list_research
    from .mission import list_missions

    index = LinkIndex()

    # state_filter=None already covers open/progress/blocked/done; union an
    # explicit rejected scan (excluded by design) so the ledger is included.
    tasks = list_tasks(config, project_id, state_filter=None)
    tasks += list_tasks(config, project_id, state_filter=TaskState.REJECTED)
    research = list_research(config, project_id)
    missions = list_missions(config, project_id)

    for task in tasks:
        index.ids.add(task.id)
        index.kinds[task.id] = "task"
    for item in research:
        index.ids.add(item.id)
        index.kinds[item.id] = "research"
    for mission in missions:
        index.ids.add(mission.id)
        index.kinds[mission.id] = "mission"

    for task in tasks:
        for target in extract_wiki_links(task.content):
            index._register(task.id, target, VIA_WIKI)
        if task.parent:
            index._register(task.id, task.parent, "parent")
        for dep in task.depends or []:
            if isinstance(dep, str):
                index._register(task.id, dep, "depends")
        for ref in task.predictions.reference_tasks or []:
            if isinstance(ref, str):
                index._register(task.id, ref, "reference")
        if task.supersedes:
            index._register(task.id, task.supersedes, "supersedes")
        if task.parent_mission:
            index._register(task.id, task.parent_mission, "mission")

    for item in research:
        for target in extract_wiki_links(item.content):
            index._register(item.id, target, VIA_WIKI)

    for mission in missions:
        for target in extract_wiki_links(mission.content):
            index._register(mission.id, target, VIA_WIKI)
        for goal in mission.mini_goals:
            index._register(mission.id, goal.id, "mini_goal")

    return index


def links_payload(index: LinkIndex, entity_id: str) -> dict[str, object]:
    """The ``{links, linked_from}`` block injected into show/context output."""
    return {
        "links": index.links_of(entity_id),
        "linked_from": index.linked_from(entity_id),
    }


def find_dangling_links(
    config: PortfolioConfig,
    project_id: str,
    index: LinkIndex | None = None,
) -> list[dict[str, str]]:
    """Report ``[[id]]`` wiki-links whose target is not a known entity id.

    Only freeform wiki-links are flagged (typed edges have their own integrity
    checks elsewhere). Project-scoped: a link to an id outside this project's
    universe is treated as dangling, consistent with clawpm's project-scoped
    isolation. Returns findings sorted by ``(source, target)``.
    """
    idx = index if index is not None else build_link_index(config, project_id)
    findings: list[dict[str, str]] = []
    for src, edges in idx.outbound.items():
        for dst, via in edges:
            if via != VIA_WIKI:
                continue
            if dst not in idx.ids:
                findings.append({
                    "project_id": project_id,
                    "source": src,
                    "target": dst,
                    "kind": idx.kinds.get(src, "unknown"),
                })
    return sorted(findings, key=lambda f: (f["source"], f["target"]))
