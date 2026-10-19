"""CLAWP-099 round 2a -- ``clawpm audit collate`` (deterministic CLI door).

The skill fans out the dimension agents; this command only validates, dedups
and maps their findings to an emit-tree document. No LLM, no network, no writes.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from clawpm.audit_findings import DEFAULT_MAX_FINDINGS
from clawpm.cli import main


def _f(dim, file, title, severity="medium", line=10, evidence="seen in review"):
    return {"dimension": dim, "file": file, "line": line, "title": title,
            "severity": severity, "evidence": evidence}


def _write(d, dim, findings):
    (d / f"{dim}.json").write_text(json.dumps(findings), encoding="utf-8")


def _text(res) -> str:
    out = res.output or ""
    try:
        out += res.stderr or ""
    except ValueError:
        pass
    return out


def _run(args, global_args=()):
    return CliRunner().invoke(main, [*global_args, "audit", "collate", *args])


def _three_dim_dir(tmp_path):
    d = tmp_path / "findings"
    d.mkdir()
    _write(d, "correctness", [
        _f("correctness", "src/a.py", "Unclosed file handle in loader", "high"),
    ])
    _write(d, "security", [
        _f("security", "src/a.py", "Unclosed file handle in loader", "medium", line=12),
        _f("security", "src/b.py", "Token logged in plaintext", "critical"),
    ])
    _write(d, "tests", [])
    return d


def test_collate_dedups_across_dimensions_and_emits_document(tmp_path):
    d = _three_dim_dir(tmp_path)
    res = _run(["--findings-dir", str(d), "--dimensions-run",
                "correctness,security,tests", "--root-title", "Audit 2026-10-20"])
    assert res.exit_code == 0, _text(res)
    out = json.loads(res.stdout)
    assert out["nothing_to_emit"] is False
    doc = out["document"]
    assert doc["root"] == {"title": "Audit 2026-10-20"}
    assert len(doc["leaves"]) == 2  # the shared defect merged
    assert doc["leaves"][0]["title"] == "Token logged in plaintext"  # critical first
    rep = out["report"]
    assert (rep["input"], rep["merged"], rep["kept"], rep["dropped"]) == (3, 2, 2, 0)
    assert rep["dimensions_run"] == ["correctness", "security", "tests"]
    assert rep["findings_per_dimension"] == {"correctness": 1, "security": 2, "tests": 0}


def test_document_passes_emit_tree_parser(tmp_path):
    from clawpm.emit_tree import parse_emit_document

    d = _three_dim_dir(tmp_path)
    res = _run(["--findings-dir", str(d), "--dimensions-run",
                "correctness,security,tests", "--root-title", "Audit"])
    parse_emit_document(json.loads(res.stdout)["document"])


def test_dispatched_dimension_with_no_file_fails_naming_it(tmp_path):
    d = _three_dim_dir(tmp_path)
    res = _run(["--findings-dir", str(d), "--dimensions-run",
                "correctness,security,tests,tooling", "--root-title", "A"])
    assert res.exit_code != 0
    text = _text(res)
    assert "tooling" in text and "no findings file" in text


def test_empty_list_for_dispatched_dimension_is_valid_and_counted(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    _write(d, "tests", [])
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests",
                "--root-title", "A"])
    assert res.exit_code == 0, _text(res)
    out = json.loads(res.stdout)
    assert out["nothing_to_emit"] is True
    assert out["document"] is None
    assert out["report"]["findings_per_dimension"] == {"tests": 0}
    assert out["report"]["input"] == 0


def test_extra_file_for_undispatched_dimension_fails(tmp_path):
    d = _three_dim_dir(tmp_path)
    _write(d, "tooling", [])
    res = _run(["--findings-dir", str(d), "--dimensions-run",
                "correctness,security,tests", "--root-title", "A"])
    assert res.exit_code != 0
    assert "tooling" in _text(res) and "not in --dimensions-run" in _text(res)


def test_malformed_json_names_the_file_without_traceback(tmp_path):
    d = _three_dim_dir(tmp_path)
    (d / "security.json").write_text("[{not json", encoding="utf-8")
    res = _run(["--findings-dir", str(d), "--dimensions-run",
                "correctness,security,tests", "--root-title", "A"])
    assert res.exit_code != 0
    assert "security.json" in _text(res)
    assert "Traceback" not in _text(res)
    assert res.exception is None or isinstance(res.exception, SystemExit)


def test_non_list_file_fails_naming_the_file(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    (d / "tests.json").write_text('{"findings": []}', encoding="utf-8")
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests",
                "--root-title", "A"])
    assert res.exit_code != 0
    assert "tests.json" in _text(res)


def test_schema_errors_list_every_problem(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    _write(d, "tests", [
        _f("tests", "a.py", "ok title", severity="bogus"),
        _f("tests", "", "another"),
    ])
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests",
                "--root-title", "A"])
    assert res.exit_code != 0
    text = _text(res)
    assert "severity" in text and "file must be a non-empty string" in text


def test_problems_are_collected_not_first_only(tmp_path):
    d = _three_dim_dir(tmp_path)
    _write(d, "tooling", [])  # extra
    res = _run(["--findings-dir", str(d), "--dimensions-run",
                "correctness,security,tests,dead_code", "--root-title", "A"])
    text = _text(res)
    assert "tooling" in text and "dead_code" in text


def test_unknown_dimension_in_run_list_fails(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    res = _run(["--findings-dir", str(d), "--dimensions-run", "vibes",
                "--root-title", "A"])
    assert res.exit_code != 0
    assert "vibes" in _text(res)


def test_zero_findings_is_nothing_to_emit_exit_zero(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    _write(d, "correctness", [])
    _write(d, "security", [])
    res = _run(["--findings-dir", str(d), "--dimensions-run", "correctness,security",
                "--root-title", "A"])
    assert res.exit_code == 0
    out = json.loads(res.stdout)
    assert out["nothing_to_emit"] is True
    assert out["report"]["nothing_to_emit"] is True


def test_max_leaves_caps_and_reports_drops(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    _write(d, "correctness", [
        _f("correctness", f"src/m{i}.py", f"distinct defect number {i} xyz{i}")
        for i in range(3)
    ])
    res = _run(["--findings-dir", str(d), "--dimensions-run", "correctness",
                "--root-title", "A", "--max-leaves", "2"])
    assert res.exit_code == 0, _text(res)
    out = json.loads(res.stdout)
    assert len(out["document"]["leaves"]) == 2
    assert out["report"]["dropped"] == 1
    assert len(out["report"]["dropped_findings"]) == 1


def test_default_cap_is_the_module_cap():
    from clawpm.cli.audit import audit_collate

    opt = next(p for p in audit_collate.params if p.name == "max_leaves")
    assert opt.default == DEFAULT_MAX_FINDINGS


def test_max_leaves_below_one_rejected(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    _write(d, "tests", [])
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests",
                "--root-title", "A", "--max-leaves", "0"])
    assert res.exit_code != 0


def test_global_format_json_before_subcommand(tmp_path):
    d = _three_dim_dir(tmp_path)
    res = _run(["--findings-dir", str(d), "--dimensions-run",
                "correctness,security,tests", "--root-title", "A"],
               global_args=("--format", "json"))
    assert res.exit_code == 0, _text(res)
    assert json.loads(res.stdout)["nothing_to_emit"] is False


def test_global_format_json_error_is_structured(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests",
                "--root-title", "A"], global_args=("--format", "json"))
    assert res.exit_code != 0
    err = json.loads(res.stderr.strip().splitlines()[-1])
    assert err["error"] == "audit_collate_failed"
    assert any("tests" in p for p in err["details"]["problems"])


def test_text_format_still_prints_json_on_stdout(tmp_path):
    # The payload is data for a pipe, not a human report: --format text must
    # not change the stdout contract.
    d = _three_dim_dir(tmp_path)
    res = _run(["--findings-dir", str(d), "--dimensions-run",
                "correctness,security,tests", "--root-title", "A"],
               global_args=("--format", "text"))
    assert res.exit_code == 0, _text(res)
    assert json.loads(res.stdout)["report"]["kept"] == 2


def test_collate_writes_nothing(tmp_path):
    d = _three_dim_dir(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    _run(["--findings-dir", str(d), "--dimensions-run",
          "correctness,security,tests", "--root-title", "A"])
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


# --- PR #132 fix round 1: accumulate every problem, reject empty options ---


def test_file_errors_do_not_hide_schema_errors_of_loaded_dimensions(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    _write(d, "tests", [{}])  # loads, but every field is invalid
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests,security",
                "--root-title", "A"], global_args=("--format", "json"))
    assert res.exit_code != 0
    err = json.loads(res.stderr.strip().splitlines()[-1])
    problems = err["details"]["problems"]
    assert any("security" in p and "no findings file" in p for p in problems)
    assert any("tests[0]" in p and "file must be a non-empty string" in p for p in problems)


def test_malformed_sibling_does_not_hide_schema_errors(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    _write(d, "tests", [_f("tests", "a.py", "ok title", severity="bogus")])
    (d / "security.json").write_text("[{not json", encoding="utf-8")
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests,security",
                "--root-title", "A"])
    text = _text(res)
    assert res.exit_code != 0
    assert "security.json" in text and "severity" in text


def test_extra_file_does_not_hide_schema_errors(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    _write(d, "tests", [{}])
    _write(d, "tooling", [])
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests",
                "--root-title", "A"])
    text = _text(res)
    assert res.exit_code != 0
    assert "not in --dimensions-run" in text and "tests[0]" in text


def _empty_tests_dir(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    _write(d, "tests", [_f("tests", "a.py", "ok title")])
    return d


def test_empty_root_title_rejected(tmp_path):
    d = _empty_tests_dir(tmp_path)
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests",
                "--root-title", ""])
    assert res.exit_code != 0
    assert "--root-title" in _text(res)
    assert not (res.stdout or "").lstrip().startswith("{")  # no document emitted


def test_whitespace_root_title_rejected(tmp_path):
    d = _empty_tests_dir(tmp_path)
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests",
                "--root-title", "   "])
    assert res.exit_code != 0
    assert "--root-title" in _text(res)


def test_root_title_is_stripped_and_document_still_parses(tmp_path):
    from clawpm.emit_tree import parse_emit_document

    d = _empty_tests_dir(tmp_path)
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests",
                "--root-title", "  Audit  "])
    assert res.exit_code == 0, _text(res)
    doc = json.loads(res.stdout)["document"]
    assert doc["root"] == {"title": "Audit"}
    parse_emit_document(doc)


def test_option_problems_accumulate_with_file_problems(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests,vibes",
                "--root-title", " ", "--max-leaves", "0"],
               global_args=("--format", "json"))
    assert res.exit_code != 0
    problems = json.loads(res.stderr.strip().splitlines()[-1])["details"]["problems"]
    joined = "\n".join(problems)
    for needle in ("--root-title", "--max-leaves", "vibes", "tests"):
        assert needle in joined, joined


def test_empty_dimensions_run_rejected(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    res = _run(["--findings-dir", str(d), "--dimensions-run", "",
                "--root-title", "A"])
    assert res.exit_code != 0
    assert "--dimensions-run" in _text(res)


def test_empty_dimensions_run_segment_rejected(tmp_path):
    d = _empty_tests_dir(tmp_path)
    for value in ("tests,,security", "tests, ", "tests,"):
        res = _run(["--findings-dir", str(d), "--dimensions-run", value,
                    "--root-title", "A"])
        assert res.exit_code != 0, value
        assert "empty segment" in _text(res), value


def test_missing_findings_dir_rejected(tmp_path):
    res = _run(["--findings-dir", str(tmp_path / "nope"), "--dimensions-run", "tests",
                "--root-title", "A"])
    assert res.exit_code != 0
    assert "Traceback" not in _text(res)


def test_findings_dir_that_is_a_file_rejected(tmp_path):
    f = tmp_path / "afile.json"
    f.write_text("[]", encoding="utf-8")
    res = _run(["--findings-dir", str(f), "--dimensions-run", "tests",
                "--root-title", "A"])
    assert res.exit_code != 0
    assert res.exception is None or isinstance(res.exception, SystemExit)


def test_empty_and_whitespace_findings_dir_rejected():
    for value in ("", "   "):
        res = _run(["--findings-dir", value, "--dimensions-run", "tests",
                    "--root-title", "A"])
        assert res.exit_code != 0, repr(value)
        assert res.exception is None or isinstance(res.exception, SystemExit)


def test_negative_max_leaves_rejected(tmp_path):
    d = _empty_tests_dir(tmp_path)
    res = _run(["--findings-dir", str(d), "--dimensions-run", "tests",
                "--root-title", "A", "--max-leaves", "-3"])
    assert res.exit_code != 0
    assert "--max-leaves" in _text(res)


def test_file_for_unknown_named_dimension_is_not_reported_as_extra(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    _write(d, "vibes", [])
    res = _run(["--findings-dir", str(d), "--dimensions-run", "vibes",
                "--root-title", "A"])
    text = _text(res)
    assert res.exit_code != 0
    assert "unknown dimension" in text and "not in --dimensions-run" not in text
