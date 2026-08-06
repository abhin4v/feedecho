"""Tests for the feed parser state tracking and item parsing."""

import pytest
from feed_parser import get_new_items, clean_text, clean_html


class TestGetNewItems:
    def test_no_last_seen_returns_empty(self):
        """First run should return empty list — don't post backlog."""
        items = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        assert get_new_items(items, None) == []

    def test_finds_new_items(self):
        items = [{"id": "3"}, {"id": "2"}, {"id": "1"}]
        new = get_new_items(items, "1")
        assert len(new) == 2
        assert new[0]["id"] == "2"
        assert new[1]["id"] == "3"

    def test_no_new_items(self):
        """When last_seen is the newest item, nothing is new."""
        items = [{"id": "2"}, {"id": "1"}]
        new = get_new_items(items, "2")
        assert new == []

    def test_last_seen_not_in_feed(self):
        """If last_seen_id is not in feed, all items are new."""
        items = [{"id": "3"}, {"id": "2"}, {"id": "1"}]
        new = get_new_items(items, "old_id")
        assert len(new) == 3

    def test_empty_items(self):
        assert get_new_items([], "last") == []

    def test_single_item_feed(self):
        items = [{"id": "1"}]
        new = get_new_items(items, "1")
        assert new == []

    def test_new_items_returned_oldest_first(self):
        """Items should be posted oldest-first (reversed from feed order)."""
        items = [{"id": "5"}, {"id": "4"}, {"id": "3"}, {"id": "2"}, {"id": "1"}]
        new = get_new_items(items, "1")
        assert [item["id"] for item in new] == ["2", "3", "4", "5"]


class TestCleanText:
    def test_strips_html_tags(self):
        assert clean_text("<p>Hello world</p>") == "Hello world"

    def test_strips_nested_tags(self):
        assert clean_text("<div><p>Hello <b>world</b></p></div>") == "Hello world"

    def test_normalizes_whitespace(self):
        assert clean_text("Hello   world\n\n  test") == "Hello world test"

    def test_empty_string(self):
        assert clean_text("") == ""

    def test_none_returns_empty(self):
        assert clean_text(None) == ""

    def test_plain_text_unchanged(self):
        assert clean_text("Just text") == "Just text"


class TestCleanHtml:
    def test_strips_scripts(self):
        html = "<p>Hello</p><script>alert('xss')</script><p>World</p>"
        result = clean_html(html)
        assert "alert" not in result
        assert "Hello" in result
        assert "World" in result

    def test_strips_styles(self):
        html = "<style>body { color: red; }</style><p>Content</p>"
        result = clean_html(html)
        assert "color" not in result
        assert "Content" in result

    def test_empty_string(self):
        assert clean_html("") == ""

    def test_preserves_content(self):
        html = "<p>Hello <a href='link'>world</a></p>"
        result = clean_html(html)
        assert "Hello" in result
        assert "world" in result
