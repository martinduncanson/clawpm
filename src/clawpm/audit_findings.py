"""CLAWP-099 — deterministic half of the project-health audit pipeline.

N analysis-dimension agents (the skill's judgment half) each report findings
as JSON. This module owns the facts: schema validation (loud, never skipped),
dedup by a stable key, severity ordering, a volume cap, and the mapping to an
emit-tree document. ZERO LLM calls; the vetting gates (constitution, won't-do
ledger) stay in ``emit_tree``.

Dedup key, within one normalised file path: title token sets (stopwords
dropped) with Jaccard >= ``TITLE_JACCARD`` -> same defect, on any line. A shared
line alone is NOT identity: unrelated defects can sit on one line.
Design: docs/design/2026-10-19-project-health-audit-pipeline.md
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

DIMENSIONS = (
    "correctness",
    "security",
    "consistency",
    "tests",
    "dead_code",
    "tooling",
    "agentic_setup",
)
SEVERITIES = ("critical", "high", "medium", "low", "info")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
_DIM_RANK = {d: i for i, d in enumerate(DIMENSIONS)}
ALLOWED_FINDING_KEYS = frozenset(
    {"dimension", "file", "line", "title", "severity", "evidence"}
)
TITLE_JACCARD = 0.6
DEFAULT_MAX_FINDINGS = 40
_STOPWORDS = frozenset(
    "a an the of in on at to for and or is are was be before after when with "
    "without not no it its this that by from as".split()
)
_EVIDENCE_IN_CRITERION = 300


class AuditFindingError(ValueError):
    """Raised when one or more findings fail schema validation."""


@dataclass(frozen=True)
class Finding:
    dimension: str
    file: str
    line: int | None
    title: str
    severity: str
    evidence: str


@dataclass(frozen=True)
class MergedFinding:
    dimensions: tuple[str, ...]
    file: str
    line: int | None
    title: str
    severity: str
    evidence: str


def _norm_path(path: str) -> str:
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return re.sub(r"/{2,}", "/", p)


def _tokens(title: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 1)


def parse_findings(by_dimension: dict[str, Any]) -> list[Finding]:
    """Validate ``{dimension: [finding, ...]}``; raise with EVERY problem listed."""
    problems: list[str] = []
    out: list[Finding] = []
    if not isinstance(by_dimension, dict):
        raise AuditFindingError("findings must be an object keyed by dimension")
    for dim, items in by_dimension.items():
        if dim not in _DIM_RANK:
            problems.append(f"unknown dimension {dim!r} (expected one of {list(DIMENSIONS)})")
            continue
        if not isinstance(items, list):
            problems.append(f"{dim}: must be a list of findings")
            continue
        for i, raw in enumerate(items):
            where = f"{dim}[{i}]"
            if not isinstance(raw, dict):
                problems.append(f"{where}: finding must be an object")
                continue
            before = len(problems)
            unknown = set(raw) - ALLOWED_FINDING_KEYS
            if unknown:
                problems.append(f"{where}: unknown keys {sorted(unknown)!r}")
            if raw.get("dimension") != dim:
                problems.append(
                    f"{where}: dimension {raw.get('dimension')!r} does not match its group {dim!r}"
                )
            for key in ("file", "title", "evidence"):
                v = raw.get(key)
                if not isinstance(v, str) or not v.strip():
                    problems.append(f"{where}: {key} must be a non-empty string")
            if raw.get("severity") not in _SEV_RANK:
                problems.append(
                    f"{where}: severity {raw.get('severity')!r} not in {list(SEVERITIES)}"
                )
            line = raw.get("line")
            if line is not None and (
                isinstance(line, bool) or not isinstance(line, int) or line < 1
            ):
                problems.append(f"{where}: line must be a positive integer or null")
            if len(problems) == before:
                out.append(Finding(
                    dimension=dim,
                    file=_norm_path(raw["file"]),
                    line=line,
                    title=" ".join(raw["title"].split()),
                    severity=raw["severity"],
                    evidence=raw["evidence"].strip(),
                ))
    if problems:
        raise AuditFindingError(
            f"{len(problems)} invalid audit finding(s):\n- " + "\n- ".join(problems)
        )
    return out


def _same_defect(a: Finding, b: Finding) -> bool:
    if a.file != b.file:
        return False
    ta, tb = _tokens(a.title), _tokens(b.title)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= TITLE_JACCARD


def _sort_key(f: Finding) -> tuple:
    # Covers every field, so the order (and the evidence merged in that order)
    # cannot depend on how the agents returned their findings.
    return (f.file, f.line or 0, _DIM_RANK[f.dimension], f.title,
            _SEV_RANK[f.severity], f.evidence)


def dedup_findings(findings: list[Finding]) -> list[MergedFinding]:
    """Cluster duplicates; output order is severity, corroboration, location."""
    clusters: list[list[Finding]] = []
    for f in sorted(findings, key=_sort_key):  # sorted first: order-independent
        for cluster in clusters:
            if _same_defect(cluster[0], f):
                cluster.append(f)
                break
        else:
            clusters.append([f])

    merged: list[MergedFinding] = []
    for cluster in clusters:
        rep = min(cluster, key=lambda f: (_SEV_RANK[f.severity], _DIM_RANK[f.dimension], f.title))
        dims = tuple(sorted({f.dimension for f in cluster}, key=_DIM_RANK.get))
        evidence = "\n".join(dict.fromkeys(f.evidence for f in cluster))
        merged.append(MergedFinding(
            dimensions=dims, file=rep.file, line=rep.line, title=rep.title,
            severity=rep.severity, evidence=evidence,
        ))
    merged.sort(key=lambda m: (_SEV_RANK[m.severity], -len(m.dimensions),
                               m.file, m.line or 0, m.title))
    return merged


def _leaf_key(m: MergedFinding) -> str:
    basis = f"{m.file}|{m.line}|{' '.join(sorted(_tokens(m.title)))}"
    return "audit-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def build_emit_document(
    findings: list[Finding],
    root_title: str,
    max_findings: int = DEFAULT_MAX_FINDINGS,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Dedup, cap and map to an emit-tree document. Returns ``(doc, report)``.

    The report names every dropped finding so the cap is never silent. A clean
    audit (no findings) is a successful no-op: ``doc`` is ``None`` and
    ``report["nothing_to_emit"]`` is true, because ``parse_emit_document``
    rejects an empty ``leaves`` list.
    """
    if max_findings < 1:
        raise ValueError("max_findings must be >= 1")
    merged = dedup_findings(findings)
    kept, dropped = merged[:max_findings], merged[max_findings:]
    leaves = []
    for n, m in enumerate(kept, 1):
        where = f"{m.file}:{m.line}" if m.line else m.file
        leaves.append({
            "ref": f"f{n:03d}",
            "parent_ref": None,
            "title": m.title,
            "leaf_key": _leaf_key(m),
            "success_criteria": [{
                "criterion": (
                    f"WHEN {where} is re-audited under the {'/'.join(m.dimensions)} "
                    f"lens THEN the defect '{m.title}' no longer reproduces"
                ),
                "gradeable_signal": m.evidence[:_EVIDENCE_IN_CRITERION],
                "comparator": "defect absent on re-check",
            }],
            "scope": [m.file],
            "delegability": "either",
        })
    doc = (
        {"schema_version": 1, "root": {"title": root_title}, "leaves": leaves}
        if leaves else None
    )
    report = {
        "nothing_to_emit": doc is None,
        "input": len(findings),
        "merged": len(merged),
        "kept": len(kept),
        "dropped": len(dropped),
        "dropped_findings": [
            {"file": m.file, "line": m.line, "title": m.title, "severity": m.severity}
            for m in dropped
        ],
    }
    return doc, report
