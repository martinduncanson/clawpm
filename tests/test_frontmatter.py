"""Tests for the canonical frontmatter parse helpers (CLAWP-079).

These pin the malformed-input contract that every migrated call site relies on:

- ``parse_frontmatter`` never raises and yields ``({}, ...)`` on failure.
- ``split_frontmatter`` raises ``FrontmatterError`` with a specific ``reason``.
"""

import pytest
import yaml

from clawpm.frontmatter import (
    FrontmatterError,
    parse_frontmatter,
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
