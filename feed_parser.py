"""Feed parser — fetch and parse RSS, Atom, and JSON feeds.

Returns normalized feed items with: id, title, link, summary, content,
author, date, and raw data for template access.
"""

import re
import hashlib
import html
import httpx
import feedparser
from datetime import datetime, timezone
from time import mktime


USER_AGENT = "feedecho/0.1 (+https://github.com/jcrabapple)"
MAX_FEED_SIZE = 10 * 1024 * 1024  # 10 MB cap


def fetch_feed(url: str) -> dict:
    """Fetch and parse a feed URL. Returns dict with feed metadata and items.

    Raises httpx.HTTPError on network failure.
    """
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=30) as client:
        response = client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")

    # Cap feed size to prevent OOM from hostile feeds
    if len(response.content) > MAX_FEED_SIZE:
        raise ValueError(f"Feed too large: {len(response.content)} bytes (max {MAX_FEED_SIZE})")

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
            "id": _get_item_id(entry),
            "title": clean_text(entry.get("title", "")),
            "link": entry.get("link", ""),
            "summary": clean_text(entry.get("summary", "")),
            "content": strip_html(entry.get("content", [{}])[0].get("value", "")) if entry.get("content") else clean_text(entry.get("summary", "")),
            "author": entry.get("author", ""),
            "date": _parse_date_struct(entry),
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
    items = []
    for entry in data.get("items", []):
        # JSON Feed 1.1 uses "authors" (array), 1.0 uses "author" (object)
        author_name = ""
        authors = entry.get("authors") or []
        if authors and isinstance(authors, list):
            author_name = authors[0].get("name", "") if isinstance(authors[0], dict) else ""
        elif entry.get("author") and isinstance(entry.get("author"), dict):
            author_name = entry["author"].get("name", "")

        item = {
            "id": _get_json_item_id(entry),
            "title": entry.get("title", ""),
            "link": entry.get("url", ""),
            "summary": entry.get("summary", ""),
            "content": strip_html(entry.get("content_html") or entry.get("content_text", "")),
            "author": author_name,
            "date": _parse_iso_date(entry.get("date_published") or entry.get("date_modified")),
            "tags": entry.get("tags", []),
            "raw": entry,
        }
        items.append(item)

    return {
        "title": data.get("title", "Unknown"),
        "url": data.get("feed_url", ""),
        "type": "json",
        "items": items,
    }


def get_new_items(items: list[dict], last_seen_id: str | None) -> list[dict]:
    """Return only items newer than last_seen_id.

    If last_seen_id is None, returns empty list (first run — don't post backlog).
    If last_seen_id is not found in the feed (scrolled off), returns the newest
    item only to prevent backlog spam.
    """
    if last_seen_id is None:
        return []
    if not items:
        return []

    new_items = []
    found_seen = False
    for item in items:
        if item["id"] == last_seen_id:
            found_seen = True
            break
        new_items.append(item)

    if not found_seen:
        # Cursor scrolled off the feed — only post the newest item to avoid spam
        return items[:1] if items else []

    # Items come newest-first in most feeds. Reverse so oldest-new-item posts first.
    new_items.reverse()
    return new_items


def clean_text(text: str) -> str:
    """Strip HTML tags, decode entities, and normalize whitespace."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_html(html_str: str) -> str:
    """Strip all HTML for plain-text output (Mastodon statuses are plain text)."""
    if not html_str:
        return ""
    # Remove script/style blocks
    html_str = re.sub(r"<script[^>]*>.*?</script>", "", html_str, flags=re.DOTALL | re.IGNORECASE)
    html_str = re.sub(r"<style[^>]*>.*?</style>", "", html_str, flags=re.DOTALL | re.IGNORECASE)
    # Strip all tags
    html_str = re.sub(r"<[^>]+>", "", html_str)
    # Decode entities
    html_str = html.unescape(html_str)
    # Normalize whitespace
    html_str = re.sub(r"\s+", " ", html_str).strip()
    return html_str


def clean_html(html_str: str) -> str:
    """Light-clean HTML: kept for backwards compat. Use strip_html for Mastodon output."""
    return strip_html(html_str)


def truncate(text: str, max_len: int = 500) -> str:
    """Truncate text to max_len chars with ellipsis."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 1].rstrip() + "…"


def _get_item_id(entry: dict) -> str:
    """Get a stable item ID, synthesizing one if the feed lacks guid/link."""
    item_id = entry.get("id") or entry.get("guid") or entry.get("link", "")
    if item_id:
        return item_id
    # Synthesize from title + date + content hash
    seed = (entry.get("title", "") + entry.get("published", "") + entry.get("summary", "")).encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def _get_json_item_id(entry: dict) -> str:
    """Get a stable item ID for JSON Feed entries."""
    item_id = entry.get("id") or entry.get("url", "")
    if item_id:
        return item_id
    seed = (entry.get("title", "") + entry.get("date_published", "") + entry.get("content_text", "")).encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def _parse_date_struct(entry: dict) -> str | None:
    """Parse date from feedparser's struct_time fields (already parsed)."""
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                dt = datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)
                return dt.isoformat()
            except (ValueError, OverflowError):
                pass
    # Fallback to raw string
    return entry.get("published") or entry.get("updated")


def _parse_iso_date(date_str: str | None) -> str | None:
    """Parse an ISO 8601 date string."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.isoformat()
    except (ValueError, TypeError):
        return date_str
