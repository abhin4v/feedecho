"""Scheduler — background feed checker that polls feeds and posts new items.

Uses APScheduler for periodic feed checking. For each feed, fetches new items,
finds matching echoes, renders templates, and dispatches to the destination
(Mastodon or email).

Key design: network I/O happens OUTSIDE DB transactions. Each item is processed
in its own short transaction to prevent lock contention and data loss.

PENDING-ROW PATTERN: each (echo, item) pair is claimed via INSERT OR IGNORE
with status='pending' before dispatch. The unique index on (echo_id, item_id)
prevents duplicate claims. After dispatch the row is UPDATEd to success/failed.
This fixes both the duplicate-post race (#3) and failed-post retry (#2): a
failed pending row will be picked up on the next poll because item_id matches,
the pending row already exists (so INSERT is ignored), and we retry.
"""

import logging
from datetime import datetime, timezone
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import get_db
from feed_parser import fetch_feed, get_new_items, truncate
from mastodon import post_status
from email_sender import send_email
from template_engine import render_template

logger = logging.getLogger("feedecho.scheduler")

scheduler: BackgroundScheduler | None = None
MASTODON_MAX_CHARS = 500


def check_feed(feed_id: int):
    """Fetch a feed, find new items, post to all enabled echoes for that feed.

    Network I/O (feed fetch + Mastodon posts / emails) happens outside DB
    transactions. Each item gets its own short transaction for cursor advance
    + post logging.

    Cursor only advances past items where all echoes succeeded. Items with
    any failed echo are retried on the next poll.
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
        _update_last_fetched(feed_id)
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
        with get_db() as db:
            db.execute(
                "UPDATE feeds SET last_item_id = ?, last_fetched = ? WHERE id = ?",
                (items[0]["id"], _now(), feed_id),
            )
        logger.info(f"Feed {feed_id} ({feed_name}): initialized last_item_id to {items[0]['id']}")
        return

    # 4. Find new items (pure computation, no DB)
    new_items = get_new_items(items, last_seen_id)

    if not new_items:
        logger.info(f"Feed {feed_id} ({feed_name}): no new items")
        _update_last_fetched(feed_id)
        return

    logger.info(f"Feed {feed_id} ({feed_name}): {len(new_items)} new item(s)")

    # 5. Process each item — only advance cursor past fully-succeeded items
    cursor_id = last_seen_id
    for item in new_items:
        all_succeeded = True
        for echo in echoes:
            if not process_echo(echo, item):
                all_succeeded = False
        if all_succeeded:
            cursor_id = item["id"]

    # 6. Advance cursor only to last fully-succeeded item
    with get_db() as db:
        db.execute(
            "UPDATE feeds SET last_item_id = ?, last_fetched = ? WHERE id = ?",
            (cursor_id, _now(), feed_id),
        )


def process_echo(echo, item: dict) -> bool:
    """Process a single echo/item pair. Returns True if fully succeeded.

    Uses pending-row pattern: INSERT OR IGNORE a 'pending' row to claim the
    (echo, item) pair. If the row already exists (from a prior attempt),
    check its status: 'pending' = claimed by another concurrent run (skip);
    'failed' = retry; 'success' = done. The unique index on (echo_id, item_id)
    prevents duplicate claims.
    """
    echo_id = echo["id"]
    item_id = item["id"]

    # Try to claim this pair
    with get_db() as db:
        db.execute(
            "INSERT OR IGNORE INTO posted_items (echo_id, item_id, item_title, item_url, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (echo_id, item_id, item.get("title", ""), item.get("link", "")),
        )
        row = db.execute(
            "SELECT id, status FROM posted_items WHERE echo_id = ? AND item_id = ?",
            (echo_id, item_id),
        ).fetchone()

    if not row:
        return False  # shouldn't happen
    if row["status"] == "success":
        return True  # already done
    if row["status"] == "pending" and db.total_changes == 0:
        # Another concurrent run claimed this pair first
        return False

    posted_id = row["id"]

    # Render template (wrapped in try/except — #4 fix)
    try:
        content = render_template(echo["template"], item)
    except Exception as e:
        logger.error(f"Echo {echo_id}: template render failed for item {item_id}: {e}")
        _update_post(posted_id, "failed", f"Template error: {e}")
        return False

    if not content.strip():
        logger.warning(f"Echo {echo_id}: rendered content is empty, skipping")
        _update_post(posted_id, "failed", "Rendered content was empty")
        return False

    # Dispatch based on destination type
    dest_type = echo["destination_type"]
    dest_id = echo["destination_id"]

    if dest_type == "mastodon":
        return _send_mastodon(echo, item, content, dest_id, posted_id)
    elif dest_type == "email":
        return _send_email_echo(echo, item, content, dest_id, posted_id)
    else:
        logger.error(f"Echo {echo_id}: unknown destination type '{dest_type}'")
        _update_post(posted_id, "failed", f"Unknown destination type: {dest_type}")
        return False


def _send_mastodon(echo, item: dict, content: str, account_id: int, posted_id: int) -> bool:
    """Post to Mastodon. Returns True on success."""
    with get_db() as db:
        account = db.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()

    if not account:
        logger.error(f"Echo {echo['id']}: account {account_id} not found")
        _update_post(posted_id, "failed", f"Account {account_id} not found")
        return False

    content = truncate(content, MASTODON_MAX_CHARS)

    try:
        post_status(
            instance=account["instance"],
            access_token=account["access_token"],
            content=content,
            visibility=echo["visibility"],
        )
        _update_post(posted_id, "success", None)
        title = item.get("title") or item.get("link", "?")
        logger.info(f"Echo {echo['id']}: posted '{title[:50]}' to {account['instance']}")
        return True
    except Exception as e:
        _update_post(posted_id, "failed", str(e))
        logger.error(f"Echo {echo['id']}: Mastodon post failed: {e}")
        return False


def _send_email_echo(echo, item: dict, content: str, email_account_id: int, posted_id: int) -> bool:
    """Send an email with the rendered template content. Returns True on success."""
    with get_db() as db:
        email_account = db.execute(
            "SELECT * FROM email_accounts WHERE id = ?", (email_account_id,)
        ).fetchone()

    if not email_account:
        logger.error(f"Echo {echo['id']}: email account {email_account_id} not found")
        _update_post(posted_id, "failed", f"Email account {email_account_id} not found")
        return False

    subject = (item.get("title") or item.get("link") or "FeedEcho: New Post")
    subject = truncate(subject, 200)

    try:
        send_email(
            to_email=email_account["email"],
            subject=subject,
            body=content,
        )
        _update_post(posted_id, "success", None)
        title = item.get("title") or item.get("link", "?")
        logger.info(f"Echo {echo['id']}: emailed '{title[:50]}' to {email_account['email']}")
        return True
    except Exception as e:
        _update_post(posted_id, "failed", str(e))
        logger.error(f"Echo {echo['id']}: email failed: {e}")
        return False


def _update_post(posted_id: int, status: str, error: str | None = None):
    """Update a post status after dispatch (own transaction)."""
    with get_db() as db:
        db.execute(
            "UPDATE posted_items SET status = ?, error_message = ? WHERE id = ?",
            (status, error, posted_id),
        )


def _update_last_fetched(feed_id: int):
    """Update last_fetched timestamp (short transaction)."""
    with get_db() as db:
        db.execute(
            "UPDATE feeds SET last_fetched = ? WHERE id = ?",
            (_now(), feed_id),
        )


def _now() -> str:
    """Current UTC time in SQLite-compatible format."""
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def check_all_feeds():
    """Check all feeds that are due based on their poll_interval."""
    with get_db() as db:
        feeds = db.execute("""
            SELECT id, name FROM feeds
            WHERE last_fetched IS NULL
               OR REPLACE(last_fetched, 'T', ' ') <= datetime('now', '-' || poll_interval || ' minutes')
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
    scheduler.add_job(check_all_feeds, 'date', id='startup_check')


def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None
        logger.info("Scheduler stopped")
