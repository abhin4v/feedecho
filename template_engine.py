"""Template engine — render feed item data into post content via variables.

Supports: {{ title }}, {{ link }}, {{ summary }}, {{ content }}, {{ author }},
{{ date }}, {{ date:iso }}, {{ date:short }}, {{ hashtags }}
"""

import re
from datetime import datetime


def render_template(template: str, item: dict) -> str:
    """Render a template string with feed item data.

    Args:
        template: Template string with {{ variable }} placeholders
        item: Feed item dict from feed_parser

    Returns:
        Rendered string ready for posting.
    """
    variables = {
        "title": item.get("title", ""),
        "link": item.get("link", ""),
        "summary": item.get("summary", ""),
        "content": item.get("content", ""),
        "author": item.get("author", ""),
        "date": item.get("date", ""),
        "date:iso": _format_date(item.get("date"), "%Y-%m-%dT%H:%M:%S"),
        "date:short": _format_date(item.get("date"), "%Y-%m-%d"),
        "hashtags": _format_hashtags(item.get("tags", [])),
    }

    def replace_var(match: re.Match) -> str:
        var_name = match.group(1).strip()
        value = variables.get(var_name, "")
        return str(value)

    return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", replace_var, template)


def _format_date(date_str: str | None, fmt: str) -> str:
    """Format a date string using the given format string."""
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except (ValueError, TypeError):
        return date_str


def _format_hashtags(tags: list[str]) -> str:
    """Format a list of tags as hashtag string."""
    if not tags:
        return ""
    hashtags = []
    for tag in tags:
        clean = re.sub(r"[^a-zA-Z0-9]", "", tag)
        if clean:
            hashtags.append(f"#{clean}")
    return " ".join(hashtags)


def available_variables() -> list[dict]:
    """Return description of available template variables for UI display."""
    return [
        {"var": "{{ title }}", "desc": "Post title"},
        {"var": "{{ link }}", "desc": "Post URL"},
        {"var": "{{ summary }}", "desc": "Post summary/excerpt"},
        {"var": "{{ content }}", "desc": "Full post content (HTML cleaned)"},
        {"var": "{{ author }}", "desc": "Author name"},
        {"var": "{{ date }}", "desc": "Publication date (raw)"},
        {"var": "{{ date:iso }}", "desc": "ISO 8601 date (2024-01-15T09:30:00)"},
        {"var": "{{ date:short }}", "desc": "Short date (2024-01-15)"},
        {"var": "{{ hashtags }}", "desc": "Feed tags as #hashtags"},
    ]
