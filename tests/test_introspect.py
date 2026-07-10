"""CLAWP-088 — `clawpm introspect` machine-readable capability listing.

The introspection document is generated purely from the live click registry,
so these tests assert the schema shape and that the serialized options match
the commands' actually-registered parameters (a fresh agent must be able to
construct any valid invocation from the output alone).
"""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.introspect import (
    INTROSPECT_SCHEMA_VERSION,
    build_command_tree,
    build_introspection,
)


@pytest.fixture
def doc() -> dict:
    """The full introspection document built from the real root group."""
    return build_introspection(main)


# ---------------------------------------------------------------------------
# Schema shape
# ---------------------------------------------------------------------------


def test_top_level_schema(doc):
    assert set(doc.keys()) == {"clawpm_version", "schema_version", "command"}
    assert doc["schema_version"] == INTROSPECT_SCHEMA_VERSION
    assert isinstance(doc["clawpm_version"], str) and doc["clawpm_version"]


def test_root_command_shape(doc):
    root = doc["command"]
    assert root["name"] == "clawpm"
    assert root["is_group"] is True
    assert isinstance(root["params"], list)
    assert isinstance(root["commands"], dict)
    for key in ("name", "help", "short_help", "hidden", "deprecated", "params"):
        assert key in root


def test_every_command_has_required_keys(doc):
    required = {"name", "help", "short_help", "hidden", "deprecated", "params", "is_group"}
    param_required = {
        "name", "kind", "opts", "type", "required", "multiple", "nargs",
        "has_default", "default",
    }
    group_required = {"invoke_without_command", "no_args_is_help", "chain", "commands"}

    def walk(node):
        assert required <= set(node.keys()), node["name"]
        for p in node["params"]:
            assert param_required <= set(p), (node["name"], p["name"])
            assert p["kind"] in ("option", "argument")
            assert isinstance(p["has_default"], bool)
        if node["is_group"]:
            assert group_required <= set(node.keys()), node["name"]
            for child in node["commands"].values():
                walk(child)

    walk(doc["command"])


def test_commands_sorted_for_diffability(doc):
    def walk(node):
        if node.get("is_group"):
            keys = list(node["commands"].keys())
            assert keys == sorted(keys), node["name"]
            for child in node["commands"].values():
                walk(child)

    walk(doc["command"])


def test_introspect_lists_itself(doc):
    assert "introspect" in doc["command"]["commands"]


def test_output_is_json_serializable_and_stable(doc):
    # Round-trips through JSON, and two builds are byte-identical (stable order).
    first = json.dumps(build_introspection(main), sort_keys=False)
    second = json.dumps(build_introspection(main), sort_keys=False)
    assert first == second
    assert json.loads(first) == doc


# ---------------------------------------------------------------------------
# Serialized params match the live registry (spot-check 5 varied commands)
# ---------------------------------------------------------------------------


def _find(doc, path):
    """Descend the command tree by a list of names, return the node."""
    node = doc["command"]
    for name in path:
        node = node["commands"][name]
    return node


def _opts_by_name(node):
    return {p["name"]: p for p in node["params"]}


def test_read_command_status_present(doc):
    # A read command with no required args serializes cleanly.
    node = _find(doc, ["status"])
    assert node["is_group"] is False
    assert isinstance(node["params"], list)


def test_choice_type_carries_choices(doc):
    # Root --format is a click.Choice(json/text).
    fmt = _opts_by_name(doc["command"])["format"]
    assert fmt["type"] == "choice"
    assert fmt["choices"] == ["json", "text"]
    assert "--format" in fmt["opts"] and "-f" in fmt["opts"]


def test_flag_option_marked(doc):
    no_hints = _opts_by_name(doc["command"])["no_hints"]
    assert no_hints["is_flag"] is True
    assert no_hints["opts"] == ["--no-hints"]


def test_mutator_tasks_add_options(doc):
    node = _find(doc, ["tasks", "add"])
    opts = _opts_by_name(node)
    # required string option
    assert opts["title"]["required"] is True
    assert "--title" in opts["title"]["opts"]
    # a Choice option
    assert opts["complexity"]["type"] == "choice"
    assert opts["complexity"]["choices"]


def test_repeatable_option_marked_multiple(doc):
    node = _find(doc, ["tasks", "add"])
    opts = _opts_by_name(node)
    # --tag is a repeatable (multiple=True) option.
    assert opts["tags"]["multiple"] is True
    assert "--tag" in opts["tags"]["opts"]


def test_serialized_params_match_registry_structurally():
    """Every serialized param — options AND positional arguments — matches its
    live click param in order, kind, required, multiple, nargs, and choices.

    Name-set equality alone wouldn't catch a positional argument whose kind or
    nargs was serialized wrongly; this proves the whole shape is generated from
    the registry, not hand-maintained."""

    def walk(cmd, node):
        # Order-preserving, not just set-equal.
        assert [p.name for p in cmd.params] == [p["name"] for p in node["params"]], node["name"]
        for live, ser in zip(cmd.params, node["params"]):
            assert ser["kind"] == live.param_type_name, (node["name"], live.name)
            assert ser["required"] == bool(live.required), (node["name"], live.name)
            assert ser["multiple"] == bool(getattr(live, "multiple", False))
            assert ser["nargs"] == live.nargs
            assert ser["opts"] == list(live.opts)
            if isinstance(live.type, click.Choice):
                assert ser["choices"] == list(live.type.choices)
        if isinstance(cmd, click.Group):
            for sub_name, sub_cmd in cmd.commands.items():
                walk(sub_cmd, node["commands"][sub_name])

    walk(main, build_command_tree(main, "clawpm"))


def test_positional_argument_serialized(doc):
    # `clawpm use [PROJECT_ID]` — a positional argument, so an agent must be able
    # to see it's an argument (not an option) with its arity.
    node = _find(doc, ["use"])
    args = [p for p in node["params"] if p["kind"] == "argument"]
    assert any(p["name"] == "project_id" for p in args)
    pid = next(p for p in args if p["name"] == "project_id")
    # A positional carries its metavar name in opts, never a "--flag" form.
    assert not any(o.startswith("-") for o in pid["opts"])
    assert pid["nargs"] == 1


def test_has_default_distinguishes_unset_from_none():
    # An UNSET (no default configured) argument reports has_default=False, while
    # a flag with an explicit default reports has_default=True — collapsing both
    # to a bare null would be lossy.
    doc = build_introspection(main)
    use_args = {p["name"]: p for p in _find(doc, ["use"])["params"]}
    assert use_args["project_id"]["has_default"] is False
    no_hints = {p["name"]: p for p in doc["command"]["params"]}["no_hints"]
    assert no_hints["has_default"] is True
    assert no_hints["default"] is False


def test_group_exposes_bare_invocation_semantics(doc):
    # `tasks` is invoke_without_command=True (bare `clawpm tasks` lists tasks).
    tasks = doc["command"]["commands"]["tasks"]
    assert tasks["is_group"] is True
    assert tasks["invoke_without_command"] is True


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_introspect_emits_valid_json():
    r = CliRunner().invoke(main, ["introspect"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert doc["command"]["name"] == "clawpm"
    assert "introspect" in doc["command"]["commands"]


def test_cli_introspect_json_even_in_text_format():
    # The listing is definitionally structured data — JSON regardless of -f.
    r = CliRunner().invoke(main, ["-f", "text", "introspect"])
    assert r.exit_code == 0, r.output
    json.loads(r.output)  # must still parse as JSON
