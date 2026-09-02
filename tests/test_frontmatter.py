"""Tests for the canonical frontmatter parse helpers (CLAWP-079, CLAWP-091).

These pin the malformed-input contract that every migrated call site relies on:

- ``parse_frontmatter`` never raises and yields ``({}, ...)`` on failure.
- ``split_frontmatter`` raises ``FrontmatterError`` with a specific ``reason``.
- ``require_mapping`` (CLAWP-091) guards mutation sites against frontmatter
  that parses successfully but to a non-mapping (a hand-edited file whose
  frontmatter is a bare YAML scalar or list), turning what would otherwise be
  a raw ``TypeError`` at the first ``frontmatter[key] = value`` into a clear,
  file-naming ``FrontmatterError``.
"""

import pytest
import yaml

from clawpm.frontmatter import (
    FrontmatterError,
    parse_frontmatter,
    require_mapping,
    split_frontmatter,
)


class TestParseFrontmatter:
    def test_valid_frontmatter(self):
        text = "---\nid: X\npriority: 3\n---\n# Title\n\nbody\n"
        data, body = parse_frontmatter(text)
        assert data == {"id": "X", "priority": 3}
        assert body == "\n# Title\n\nbody\n"

    def test_empty_frontmatter_block(self):
        text = "---\n---\n# Title\n"
        data, body = parse_frontmatter(text)
        assert data == {}
        assert body == "\n# Title\n"

    def test_no_fence_returns_whole_text_as_body(self):
        text = "# Title\n\nno frontmatter here\n"
        data, body = parse_frontmatter(text)
        assert data == {}
        assert body == text

    def test_unterminated_fence_returns_whole_text(self):
        # Opens with --- but never closes it.
        text = "---\nid: X\npriority: 3\n# Title\n\nbody\n"
        data, body = parse_frontmatter(text)
        assert data == {}
        assert body == text

    def test_unparseable_yaml_drops_data_keeps_body(self):
        # Fenced, but the YAML body is invalid; the body after the fence is kept
        # so a rewrite caller does not rebuild a double-frontmatter file.
        text = "---\nthis: [unbalanced\n---\n# Title\n\nbody\n"
        data, body = parse_frontmatter(text)
        assert data == {}
        assert body == "\n# Title\n\nbody\n"

    def test_never_raises_on_garbage(self):
        # Even wholly invalid input must not raise.
        assert parse_frontmatter("") == ({}, "")


class TestSplitFrontmatter:
    def test_valid_frontmatter(self):
        text = "---\nid: X\npriority: 3\n---\n# Title\n\nbody\n"
        data, body = split_frontmatter(text)
        assert data == {"id": "X", "priority": 3}
        assert body == "\n# Title\n\nbody\n"

    def test_empty_frontmatter_block(self):
        data, body = split_frontmatter("---\n---\n# Title\n")
        assert data == {}
        assert body == "\n# Title\n"

    def test_absent_raises(self):
        with pytest.raises(FrontmatterError) as exc:
            split_frontmatter("# Title\n\nno frontmatter\n")
        assert exc.value.reason == "absent"

    def test_unterminated_raises(self):
        with pytest.raises(FrontmatterError) as exc:
            split_frontmatter("---\nid: X\n# Title\n\nbody\n")
        assert exc.value.reason == "unterminated"

    def test_unparseable_raises_and_chains(self):
        with pytest.raises(FrontmatterError) as exc:
            split_frontmatter("---\nthis: [unbalanced\n---\n# Title\n")
        assert exc.value.reason == "unparseable"
        assert isinstance(exc.value.__cause__, yaml.YAMLError)

    def test_frontmatter_error_is_value_error(self):
        # Existing `except ValueError` handlers must keep catching it.
        assert issubclass(FrontmatterError, ValueError)

    def test_list_frontmatter_raises_not_a_mapping(self):
        # Hand-edited file whose frontmatter is a bare YAML list.
        text = "---\n- a\n- b\n---\n# Title\n"
        with pytest.raises(FrontmatterError) as exc:
            split_frontmatter(text)
        assert exc.value.reason == "not_a_mapping"
        assert "list" in str(exc.value)

    def test_scalar_frontmatter_raises_not_a_mapping(self):
        # Hand-edited file whose frontmatter is a bare YAML scalar string.
        text = "---\njust a string\n---\n# Title\n"
        with pytest.raises(FrontmatterError) as exc:
            split_frontmatter(text)
        assert exc.value.reason == "not_a_mapping"
        assert "str" in str(exc.value)

    def test_int_frontmatter_raises_not_a_mapping(self):
        text = "---\n42\n---\n# Title\n"
        with pytest.raises(FrontmatterError) as exc:
            split_frontmatter(text)
        assert exc.value.reason == "not_a_mapping"

    def test_none_frontmatter_is_lenient_empty_dict(self):
        # An empty/None-valued block (`or {}`) is NOT a "not_a_mapping" case —
        # it coerces to {} same as before, matching the empty-block test above.
        data, _ = split_frontmatter("---\nnull\n---\n# Title\n")
        assert data == {}


class TestRequireMapping:
    def test_dict_passes_through_unchanged(self):
        d = {"id": "X"}
        assert require_mapping(d) is d

    def test_list_raises_not_a_mapping(self):
        with pytest.raises(FrontmatterError) as exc:
            require_mapping(["a", "b"])
        assert exc.value.reason == "not_a_mapping"
        assert "list" in str(exc.value)

    def test_scalar_raises_not_a_mapping(self):
        with pytest.raises(FrontmatterError) as exc:
            require_mapping("just a string")
        assert exc.value.reason == "not_a_mapping"

    def test_error_names_the_file_when_where_given(self):
        with pytest.raises(FrontmatterError) as exc:
            require_mapping([1, 2], where="/tmp/TASK-001.md")
        assert "/tmp/TASK-001.md" in str(exc.value)

    def test_omitted_where_still_raises_cleanly(self):
        with pytest.raises(FrontmatterError):
            require_mapping([1, 2])
