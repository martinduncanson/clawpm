"""Tests for CLAWP-099 — audit finding normalise + dedup + emit-tree mapping.

Deterministic half of the project-health audit pipeline: N dimension agents
report findings; this module validates, dedups, caps and maps them to an
emit-tree document. Model judgment (what to report, vetting) stays in the skill.
"""

from __future__ import annotations

import json

import pytest

from clawpm.audit_findings import (
    DIMENSIONS,
    AuditFindingError,
    build_emit_document,
    dedup_findings,
    parse_findings,
)
from clawpm.emit_tree import parse_emit_document


def _f(dimension="correctness", file="src/clawpm/tasks.py", line=10,
       title="Lock is released before write completes", severity="high",
       evidence="tasks.py:10 releases file_lock then writes"):
    return {
        "dimension": dimension, "file": file, "line": line,
        "title": title, "severity": severity, "evidence": evidence,
    }


def test_dimensions_cover_the_seven_audit_lenses():
    assert set(DIMENSIONS) == {
        "correctness", "security", "consistency", "tests",
        "dead_code", "tooling", "agentic_setup",
    }


def test_same_file_line_same_defect_from_two_dimensions_merges_to_one():
    found = parse_findings({
        "correctness": [_f("correctness", severity="medium")],
        "security": [_f("security", title="Lock released before the write completes",
                        severity="high")],
    })
    merged = dedup_findings(found)
    assert len(merged) == 1
    m = merged[0]
    assert m.dimensions == ("correctness", "security")
    assert m.severity == "high"  # highest wins
    assert "tasks.py:10" in m.evidence


def test_same_defect_different_wording_and_line_merges():
    a = _f("correctness", line=40, title="Lock released before the write completes")
    b = _f("tests", line=44, title="Write completes after lock released",
           evidence="no test covers it")
    merged = dedup_findings(parse_findings({"correctness": [a], "tests": [b]}))
    assert len(merged) == 1
    assert merged[0].dimensions == ("correctness", "tests")


def test_different_defects_in_same_file_stay_separate():
    a = _f(line=10, title="Lock released before write completes")
    b = _f(line=200, title="Unused helper parse_legacy_ids never called",
           dimension="dead_code")
    merged = dedup_findings(parse_findings({"correctness": [a], "dead_code": [b]}))
    assert len(merged) == 2


def test_different_defects_on_the_same_file_and_line_stay_separate():
    a = _f("correctness", line=10, title="Lock released before write completes")
    b = _f("security", line=10, title="Token logged in plaintext",
           severity="critical", evidence="tasks.py:10 logs the bearer token")
    merged = dedup_findings(parse_findings({"correctness": [a], "security": [b]}))
    assert len(merged) == 2
    assert {m.title for m in merged} == {a["title"], b["title"]}
    keys = [lf["leaf_key"] for lf in build_emit_document(
        parse_findings({"correctness": [a], "security": [b]}), root_title="R")[0]["leaves"]]
    assert len(set(keys)) == 2


def test_path_spelling_is_normalised_for_the_key():
    a = _f(file="src\\clawpm\\tasks.py", line=10)
    b = _f("security", file="./src/clawpm/tasks.py", line=10)
    merged = dedup_findings(parse_findings({"correctness": [a], "security": [b]}))
    assert len(merged) == 1
    assert merged[0].file == "src/clawpm/tasks.py"


def test_empty_dimension_is_fine_and_all_empty_gives_no_findings():
    assert dedup_findings(parse_findings({"security": []})) == []
    assert dedup_findings(parse_findings({})) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda f: f.pop("file"), "file"),
    (lambda f: f.update(severity="urgent"), "severity"),
    (lambda f: f.update(line="ten"), "line"),
    (lambda f: f.update(title="  "), "title"),
    (lambda f: f.update(evidence=""), "evidence"),
    (lambda f: f.update(extra="x"), "unknown"),
])
def test_malformed_finding_is_rejected_loudly(mutate, needle):
    bad = _f()
    mutate(bad)
    with pytest.raises(AuditFindingError) as exc:
        parse_findings({"correctness": [bad]})
    assert needle in str(exc.value)


def test_unknown_dimension_and_dimension_mismatch_rejected():
    with pytest.raises(AuditFindingError, match="dimension"):
        parse_findings({"vibes": [_f("vibes")]})
    with pytest.raises(AuditFindingError, match="dimension"):
        parse_findings({"security": [_f("correctness")]})


def test_all_problems_reported_not_just_first():
    with pytest.raises(AuditFindingError) as exc:
        parse_findings({"correctness": [_f(severity="x"), _f(evidence="")]})
    assert "severity" in str(exc.value) and "evidence" in str(exc.value)


def test_cap_keeps_highest_severity_then_corroboration_and_reports_drops():
    findings = {"correctness": [
        _f(file=f"src/m{i}.py", line=i + 1, title=f"Distinct defect number {i} xq{i}",
           severity="low") for i in range(5)
    ] + [_f(file="src/crit.py", line=1, title="Data loss on crash", severity="critical")]}
    doc, report = build_emit_document(
        parse_findings(findings), root_title="Audit 2026-10-19", max_findings=3)
    assert len(doc["leaves"]) == 3
    assert doc["leaves"][0]["title"].endswith("Data loss on crash")
    assert report["dropped"] == 3 and report["kept"] == 3


def test_emit_document_validates_against_emit_tree_schema():
    merged = parse_findings({
        "correctness": [_f()],
        "security": [_f("security", file="src/clawpm/serve.py", line=5,
                        title="Token logged in plaintext", severity="critical")],
    })
    doc, report = build_emit_document(merged, root_title="Audit 2026-10-19")
    parsed = parse_emit_document(doc)  # raises on any schema violation
    assert len(parsed.leaves) == 2
    assert report["dropped"] == 0
    assert all(lf.scope for lf in parsed.leaves)
    assert all(lf.success_criteria for lf in parsed.leaves)


def test_identical_location_and_title_output_is_independent_of_input_order():
    # Same file/line/dimension/title/severity, different evidence: the
    # emitted gradeable_signal is truncated, so order must not pick the winner.
    items = [
        _f(evidence="evidence-A " + "a" * 400),
        _f(evidence="evidence-B " + "b" * 400),
        _f(evidence="evidence-C " + "c" * 400),
    ]
    forward, _ = build_emit_document(
        parse_findings({"correctness": items}), root_title="R")
    backward, _ = build_emit_document(
        parse_findings({"correctness": list(reversed(items))}), root_title="R")
    assert json.dumps(forward, sort_keys=True) == json.dumps(backward, sort_keys=True)
    assert json.dumps(forward) == json.dumps(backward)  # key order too


def test_zero_findings_is_an_explicit_noop_not_an_invalid_document():
    doc, report = build_emit_document(parse_findings({"security": []}), root_title="R")
    assert doc is None
    assert report["input"] == 0 and report["kept"] == 0
    assert report["nothing_to_emit"] is True


def test_non_empty_result_reports_something_to_emit_and_parses():
    doc, report = build_emit_document(parse_findings({"correctness": [_f()]}),
                                      root_title="R")
    assert report["nothing_to_emit"] is False
    parse_emit_document(doc)  # real parser accepts it


def test_leaf_keys_are_stable_across_runs_and_input_order():
    a, b = _f(), _f("security", file="src/clawpm/serve.py", line=5,
                    title="Token logged in plaintext")
    d1, _ = build_emit_document(parse_findings({"correctness": [a], "security": [b]}),
                                root_title="R")
    d2, _ = build_emit_document(parse_findings({"security": [b], "correctness": [a]}),
                                root_title="R")
    assert [lf["leaf_key"] for lf in d1["leaves"]] == [lf["leaf_key"] for lf in d2["leaves"]]
    assert [lf["ref"] for lf in d1["leaves"]] == [lf["ref"] for lf in d2["leaves"]]
