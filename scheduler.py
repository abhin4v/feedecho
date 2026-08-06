"""Scheduler — background feed checker that polls feeds and posts new items.

Uses APScheduler for periodic feed checking. For each feed, fetches new items,
finds matching echoes, renders templates, and posts to Mastodon.

Key design: network I/O happens OUTSIDE DB transactions. Each item is processed
in its own short transaction to prevent lock contention and data loss.
"""

import logging
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import get_db
from feed_parser import fetch_feed, get_new_items, truncate
from mastodon import post_status
from template_engine import render_template

logger = logging.getLogger("feedecho.scheduler")

scheduler: BackgroundScheduler | None = None
MASTODON_MAX_CHARS = 500


def check_feed(feed_id: int):
    """Fetch a feed, find new items, post to all enabled echoes for that feed.

    Network I/O (feed fetch + Mastodon posts) happens outside DB transactions.
    Each item gets its own short transaction for cursor advance + post logging.
    """
    # 1. Read feed config (short transaction)
    with get_db() as db:
        feed = db.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if not feed:
            logger.warning(f"Feed {feed_id} not found")
            return
        echoes = db.execute(
            "SELECT * FROM echoes WHERE feed_id = ? AND enabled = 1", (feed_id,)
        ).fetchall()
        feed_url = feed["url"]
        last_seen_id = feed["last_item_id"]
        feed_name = feed["name"]

    if not echoes:
        logger.info(f"Feed {feed_id} ({feed_name}): no enabled echoes, skipping")
        return

    # 2. Fetch feed (network I/O, no DB lock)
    try:
        feed_data = fetch_feed(feed_url)
    except Exception as e:
        logger.error(f"Feed {feed_id} ({feed_name}): fetch failed: {e}")
        _update_last_fetched(feed_id)
        return

    items = feed_data["items"]
    if not items:
        logger.info(f"Feed {feed_id} ({feed_name}): no items in feed")
        _update_last_fetched(feed_id)
        return

    # 3. Auto-initialize on first fetch
    if last_seen_id is None:
        new_last_id = items[0]["id"] if items else None
        with get_db() as db:
            db.execute(
                "UPDATE feeds SET last_item_id = ?, last_fetched = ? WHERE id = ?",
                (new_last_id, datetime.now(timezone.utc).isoformat(), feed_id),
            )
        logger.info(f"Feed {feed_id} ({feed_name}): initialized last_item_id to {new_last_id}")
        return

    # 4. Find new items (pure computation, no DB)
    new_items = get_new_items(items, last_seen_id)

    if not new_items:
        logger.info(f"Feed {feed_id} ({feed_name}): no new items")
        _update_last_fetched(feed_id)
        return

    logger.info(f"Feed {feed_id} ({feed_name}): {len(new_items)} new item(s)")

    # 5. Process each item
    for item in new_items:
        for echo in echoes:
            process_echo(echo, item)

    # 6. Update last_item_id (short transaction)
    new_last_id = items[0]["id"] if items else last_seen_id
    with get_db() as db:
        db.execute(
            "UPDATE feeds SET last_item_id = ?, last_fetched = ? WHERE id = ?",
            (new_last_id, datetime.now(timezone.utc).isoformat(), feed_id),
        )


def process_echo(echo, item: dict):
    """Render template and post to Mastodon for a single echo/item pair.

    Uses per-item transactions. Only advances cursor and logs after processing.
    """
    # Read account (short transaction)
    with get_db() as db:
        account = db.execute(
            "SELECT * FROM accounts WHERE id = ?", (echo["account_id"],)
        ).fetchone()
        # Check if already posted successfully (idempotency — only success counts)
        already_posted = db.execute(
            "SELECT id FROM posted_items WHERE echo_id = ? AND item_id = ? AND status = 'success'",
            (echo["id"], item["id"]),
        ).fetchone()

    if not account:
        logger.error(f"Echo {echo['id']}: account {echo['account_id']} not found")
        return
    if already_posted:
        logger.info(f"Echo {echo['id']}: item {item['id']} already posted successfully, skipping")
        return

    # Render template (no DB)
    content = render_template(echo["template"], item)
    content = truncate(content, MASTODON_MAX_CHARS)

    if not content.strip():
        logger.warning(f"Echo {echo['id']}: rendered content is empty, skipping")
        _log_post(echo["id"], item, "failed", "Rendered content was empty")
        return

    # Post to Mastodon (network I/O, no DB lock)
    try:
        result = post_status(
            instance=account["instance"],
            access_token=account["access_token"],
            content=content,
            visibility=echo["visibility"],
        )
        _log_post(echo["id"], item, "success", None)
        logger.info(f"Echo {echo['id']}: posted '{item['title'][:50]}' to {account['instance']}")
    except Exception as e:
        _log_post(echo["id"], item, "failed", str(e))
        logger.error(f"Echo {echo['id']}: post failed: {e}")


def _log_post(echo_id: int, item: dict, status: str, error: str | None = None):
    """Log a post attempt to posted_items table (own transaction)."""
    with get_db() as db:
        db.execute(
            """INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status, error_message)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (echo_id, item["id"], item.get("title", ""), item.get("link", ""), status, error),
        )


def _update_last_fetched(feed_id: int):
    """Update last_fetched timestamp (short transaction)."""
    with get_db() as db:
        db.execute(
            "UPDATE feeds SET last_fetched = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), feed_id),
        )


def check_all_feeds():
    """Check all feeds that are due based on their poll_interval."""
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        # Respect per-feed poll_interval
        feeds = db.execute("""
            SELECT id, name FROM feeds
            WHERE last_fetched IS NULL
               OR last_fetched <= datetime('now', '-' || poll_interval || ' minutes')
        """).fetchall()

    logger.info(f"Checking {len(feeds)} feed(s)")
    for feed in feeds:
        try:
            check_feed(feed["id"])
        except Exception as e:
            logger.error(f"Error checking feed {feed['id']} ({feed['name']}): {e}")


def start_scheduler():
    """Start the background scheduler with a periodic feed checker."""
    global scheduler
    if scheduler is not None:
        return

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        check_all_feeds,
        trigger=IntervalTrigger(minutes=2),
        id="check_all_feeds",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — checking feeds every 2 minutes")
    # Run once at startup
    scheduler.add_job(check_all_feeds, 'date', id='startup_check')


def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None
        logger.info("Scheduler stopped")
