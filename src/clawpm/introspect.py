"""Machine-readable introspection of the clawpm click command tree (CLAWP-088).

Agents drive clawpm as a JSON-first CLI, but until now the only way to discover
what commands, groups, options, and choices exist was to shell ``--help`` per
group and parse human prose, or read the source. This module walks the *live*
click registry and serializes it to a stable-ordered dict, so the capability
listing is generated purely from what is actually registered and can never
drift from reality. That same property makes it the mechanism a doc-staleness
CI check (CLAWP-097) can diff against.

The walk is a pure function over a ``click.Command``; the ``clawpm introspect``
CLI command (in :mod:`clawpm.cli.introspect`) is a thin wrapper that hands it
the populated root group.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import click

try:  # click >= 8.2 marks "no default configured" with this sentinel.
    from click.core import UNSET as _UNSET
except ImportError:  # pragma: no cover — click < 8.2 used None for "unset".
    _UNSET = object()  # a private sentinel that no real default can equal.

# Bump when the emitted schema shape changes in a way consumers must notice.
INTROSPECT_SCHEMA_VERSION = 1


def _type_name(param_type: click.ParamType) -> str:
    """Best-effort stable name for a click param type.

    Built-in types expose a ``name`` ("text", "integer", "choice", …); fall
    back to the class name for custom types that don't.
    """
    return getattr(param_type, "name", None) or type(param_type).__name__


def _serialize_default(default: Any) -> tuple[bool, Any]:
    """Represent a param default as ``(has_default, value)``.

    ``has_default`` distinguishes "no default configured" (click's ``UNSET``
    sentinel) from a default that genuinely *is* ``None`` — collapsing both to a
    bare ``null`` would be lossy, since an agent reconstructing an invocation
    can't tell an omittable-defaults-to-None option from a required one. When a
    default exists but isn't JSON-native, we preserve a marker/repr rather than
    fabricate ``None``:

      - ``UNSET`` sentinel        → ``(False, None)`` — nothing configured
      - callable (resolved lazily) → ``(True, "<dynamic>")``
      - other non-serializable     → ``(True, repr(default))`` — faithful, opaque
      - JSON-native value          → ``(True, value)``
    """
    if default is _UNSET:
        return False, None
    if callable(default):
        return True, "<dynamic>"
    try:
        json.dumps(default)
    except (TypeError, ValueError):
        return True, repr(default)
    return True, default


def _serialize_param(param: click.Parameter) -> dict[str, Any]:
    """Serialize a single option/argument to the fields an agent needs to
    construct a valid invocation."""
    ptype = param.type
    has_default, default_value = _serialize_default(param.default)
    entry: dict[str, Any] = {
        "name": param.name,
        "kind": param.param_type_name,  # "option" | "argument"
        "opts": list(param.opts),
        "secondary_opts": list(param.secondary_opts),
        "type": _type_name(ptype),
        "required": bool(param.required),
        "multiple": bool(getattr(param, "multiple", False)),
        "nargs": param.nargs,
        "has_default": has_default,
        "default": default_value,
    }

    if isinstance(ptype, click.Choice):
        entry["choices"] = list(ptype.choices)

    if isinstance(param, click.Option):
        entry["is_flag"] = bool(param.is_flag)
        entry["count"] = bool(param.count)
        entry["help"] = param.help
        entry["hidden"] = bool(param.hidden)
        # envvar can be a str, a list, or None — normalize to a list for a
        # stable schema shape.
        if param.envvar is None:
            entry["envvar"] = []
        elif isinstance(param.envvar, (list, tuple)):
            entry["envvar"] = list(param.envvar)
        else:
            entry["envvar"] = [param.envvar]

    return entry


def build_command_tree(command: click.Command, name: str | None = None) -> dict[str, Any]:
    """Recursively serialize a click command/group to a stable-ordered dict.

    Groups are recursed into via ``command.commands``, with subcommands sorted
    alphabetically by name for diffable output. Parameters are kept in
    registration order: that order is deterministic *and* it preserves the
    positional semantics of arguments (sorting them would misrepresent the
    invocation).
    """
    # ``help`` is a str or None in practice, but guard cleandoc against a
    # non-str slipping through (a lazy/None-like help object) so one odd command
    # can't crash the whole walk.
    help_text = command.help if isinstance(command.help, str) else None
    node: dict[str, Any] = {
        "name": name if name is not None else command.name,
        "help": inspect.cleandoc(help_text) if help_text else None,
        "short_help": command.get_short_help_str() or None,
        "hidden": bool(command.hidden),
        "deprecated": bool(command.deprecated),
        "params": [_serialize_param(p) for p in command.params],
    }

    if isinstance(command, click.Group):
        node["is_group"] = True
        # A group can be a valid *leaf* invocation on its own — e.g. bare
        # ``clawpm tasks`` lists tasks because the group sets
        # ``invoke_without_command=True``. Surface that so an agent doesn't
        # assume every group node must be descended into to be invoked.
        node["invoke_without_command"] = bool(command.invoke_without_command)
        node["no_args_is_help"] = bool(command.no_args_is_help)
        node["chain"] = bool(command.chain)
        node["commands"] = {
            sub: build_command_tree(command.commands[sub], sub)
            for sub in sorted(command.commands)
        }
    else:
        node["is_group"] = False

    return node


def build_introspection(root: click.Command, root_name: str = "clawpm") -> dict[str, Any]:
    """Build the full introspection document rooted at *root*.

    Imported lazily to avoid a circular import at module load: ``__version__``
    lives in the ``clawpm`` package, which pulls in the CLI.
    """
    from clawpm import __version__

    return {
        "clawpm_version": __version__,
        "schema_version": INTROSPECT_SCHEMA_VERSION,
        "command": build_command_tree(root, root_name),
    }
