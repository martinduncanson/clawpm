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

    def walk(node):
        assert required <= set(node.keys()), node["name"]
        for p in node["params"]:
            assert {"name", "kind", "opts", "type", "required", "multiple", "nargs"} <= set(p)
            assert p["kind"] in ("option", "argument")
        if node["is_group"]:
            assert "commands" in node
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


def test_serialized_options_match_registry():
    """Every serialized command's option set equals the command's live params —
    proves the listing is generated from the registry, not hand-maintained."""

    def registered_opt_names(cmd):
        return {p.name for p in cmd.params}

    def walk(cmd, node):
        assert registered_opt_names(cmd) == {p["name"] for p in node["params"]}, node["name"]
        if isinstance(cmd, click.Group):
            for sub_name, sub_cmd in cmd.commands.items():
                walk(sub_cmd, node["commands"][sub_name])

    walk(main, build_command_tree(main, "clawpm"))


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
