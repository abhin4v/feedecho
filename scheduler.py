"""Scheduler — background feed checker that polls feeds and posts new items.

Uses APScheduler for periodic feed checking. For each feed, fetches new items,
finds matching echoes, renders templates, and posts to Mastodon.
"""

import logging
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import get_db
from feed_parser import fetch_feed, get_new_items
from mastodon import post_status
from template_engine import render_template

logger = logging.getLogger("feedecho.scheduler")

scheduler: BackgroundScheduler | None = None


def check_feed(feed_id: int):
    """Fetch a feed, find new items, post to all enabled echoes for that feed."""
    from database import get_db

    with get_db() as db:
        feed = db.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if not feed:
            logger.warning(f"Feed {feed_id} not found")
            return

        echoes = db.execute(
            "SELECT * FROM echoes WHERE feed_id = ? AND enabled = 1", (feed_id,)
        ).fetchall()

        if not echoes:
            logger.info(f"Feed {feed_id} ({feed['name']}): no enabled echoes, skipping")
            return

        # Fetch the feed
        try:
            feed_data = fetch_feed(feed["url"])
        except Exception as e:
            logger.error(f"Feed {feed_id} ({feed['name']}): fetch failed: {e}")
            return

        items = feed_data["items"]
        if not items:
            logger.info(f"Feed {feed_id} ({feed['name']}): no items in feed")
            return

        last_seen_id = feed["last_item_id"]
        new_items = get_new_items(items, last_seen_id)

        if not new_items:
            logger.info(f"Feed {feed_id} ({feed['name']}): no new items")
            # Still update last_fetched timestamp
            db.execute(
                "UPDATE feeds SET last_fetched = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), feed_id),
            )
            return

        logger.info(f"Feed {feed_id} ({feed['name']}): {len(new_items)} new item(s)")

        for item in new_items:
            for echo in echoes:
                process_echo(db, echo, item)

        # Update last_item_id to the newest item (items[0] is newest in most feeds)
        new_last_id = items[0]["id"] if items else last_seen_id
        db.execute(
            "UPDATE feeds SET last_item_id = ?, last_fetched = ? WHERE id = ?",
            (new_last_id, datetime.now(timezone.utc).isoformat(), feed_id),
        )


def process_echo(db, echo, item: dict):
    """Render template and post to Mastodon for a single echo/item pair."""
    account = db.execute(
        "SELECT * FROM accounts WHERE id = ?", (echo["account_id"],)
    ).fetchone()
    if not account:
        logger.error(f"Echo {echo['id']}: account {echo['account_id']} not found")
        return

    # Check if already posted (idempotency)
    already_posted = db.execute(
        "SELECT id FROM posted_items WHERE echo_id = ? AND item_id = ?",
        (echo["id"], item["id"]),
    ).fetchone()
    if already_posted:
        logger.info(f"Echo {echo['id']}: item {item['id']} already posted, skipping")
        return

    # Render the template
    content = render_template(echo["template"], item)

    if not content.strip():
        logger.warning(f"Echo {echo['id']}: rendered content is empty, skipping")
        log_post(db, echo["id"], item, "failed", "Rendered content was empty")
        return

    # Post to Mastodon
    try:
        result = post_status(
            instance=account["instance"],
            access_token=account["access_token"],
            content=content,
            visibility=echo["visibility"],
        )
        log_post(db, echo["id"], item, "success", None, result.get("url"))
        logger.info(f"Echo {echo['id']}: posted '{item['title'][:50]}' to {account['instance']}")
    except Exception as e:
        log_post(db, echo["id"], item, "failed", str(e))
        logger.error(f"Echo {echo['id']}: post failed: {e}")


def log_post(db, echo_id: int, item: dict, status: str, error: str | None = None, post_url: str | None = None):
    """Log a post attempt to posted_items table."""
    db.execute(
        """INSERT INTO posted_items (echo_id, item_id, item_title, item_url, status, error_message)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (echo_id, item["id"], item.get("title", ""), item.get("link", ""), status, error),
    )


def check_all_feeds():
    """Check all feeds that are due based on their poll_interval."""
    with get_db() as db:
        feeds = db.execute("SELECT id, name FROM feeds").fetchall()

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
        trigger=IntervalTrigger(minutes=5),
        id="check_all_feeds",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — checking feeds every 5 minutes")


def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None
        logger.info("Scheduler stopped")
