"""Feed parser — fetch and parse RSS, Atom, and JSON feeds.

Returns normalized feed items with: id, title, link, summary, content,
author, date, and raw data for template access.
"""

import httpx
import feedparser
from datetime import datetime, timezone
from typing import Optional


USER_AGENT = "feedecho/0.1 (+https://github.com/jcrabapple)"


def fetch_feed(url: str) -> dict:
    """Fetch and parse a feed URL. Returns dict with feed metadata and items.

    Raises httpx.HTTPError on network failure.
    """
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=30) as client:
        response = client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")

    # JSON Feed
    if "json" in content_type or url.endswith(".json"):
        return parse_json_feed(response.json())

    # RSS/Atom via feedparser
    parsed = feedparser.parse(response.content)
    return parse_rss_feed(parsed, url)


def parse_rss_feed(parsed: feedparser.FeedParserDict, url: str) -> dict:
    """Parse an RSS/Atom feed from feedparser output."""
    feed_info = parsed.get("feed", {})

    items = []
    for entry in parsed.get("entries", []):
        item = {
            "id": entry.get("id") or entry.get("guid") or entry.get("link", ""),
            "title": clean_text(entry.get("title", "")),
            "link": entry.get("link", ""),
            "summary": clean_text(entry.get("summary", "")),
            "content": clean_html(entry.get("content", [{}])[0].get("value", "")) if entry.get("content") else clean_text(entry.get("summary", "")),
            "author": entry.get("author", ""),
            "date": parse_date(entry.get("published") or entry.get("updated")),
            "tags": [tag.get("term", "") for tag in entry.get("tags", []) if tag.get("term")],
            "raw": {k: v for k, v in entry.items()},
        }
        items.append(item)

    return {
        "title": feed_info.get("title", url),
        "url": url,
        "type": "rss",
        "items": items,
    }


def parse_json_feed(data: dict) -> dict:
    """Parse a JSON Feed (https://jsonfeed.org/)."""
    feed_info = data.get("feed", {})

    items = []
    for entry in data.get("items", []):
        item = {
            "id": entry.get("id") or entry.get("url", ""),
            "title": entry.get("title", ""),
            "link": entry.get("url", ""),
            "summary": entry.get("summary", ""),
            "content": clean_html(entry.get("content_html") or entry.get("content_text", "")),
            "author": entry.get("author", {}).get("name", ""),
            "date": parse_date(entry.get("date_published") or entry.get("date_modified")),
            "tags": entry.get("tags", []),
            "raw": entry,
        }
        items.append(item)

    return {
        "title": feed_info.get("title", "Unknown"),
        "url": data.get("feed_url", ""),
        "type": "json",
        "items": items,
    }


def get_new_items(items: list[dict], last_seen_id: str | None) -> list[dict]:
    """Return only items newer than last_seen_id.

    If last_seen_id is None, returns empty list (first run — don't post backlog).
    """
    if last_seen_id is None:
        return []

    new_items = []
    found_seen = False
    for item in items:
        if item["id"] == last_seen_id:
            found_seen = True
            break
        new_items.append(item)

    # Items come newest-first in most feeds. Reverse so oldest-new-item posts first.
    new_items.reverse()
    return new_items


def clean_text(text: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    if not text:
        return ""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_html(html: str) -> str:
    """Light-clean HTML: keep it readable but strip scripts/styles."""
    if not html:
        return ""
    import re
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    return html.strip()


def parse_date(date_str: str | None) -> str | None:
    """Parse a date string to ISO format."""
    if not date_str:
        return None
    try:
        parsed = feedparser.parse_date(date_str) if date_str else None
        if parsed:
            return parsed.isoformat()
    except Exception:
        pass
    return date_str
