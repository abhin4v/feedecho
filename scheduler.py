"""Background scheduler and concurrency-safe feed delivery pipeline."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from database import get_db
from email_sender import send_email
from feed_parser import fetch_feed, get_new_items, truncate
from filters import is_filtered
from mastodon import post_status
from notify import (
    max_attempts,
    next_retry_delay,
    record_failure,
    record_success,
)
from template_engine import render_template

logger = logging.getLogger("feedecho.scheduler")

scheduler: BackgroundScheduler | None = None

MASTODON_MAX_CHARS = 500
PENDING_RECLAIM_SECONDS = 10 * 60
FEED_LEASE_SECONDS = 15 * 60


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _timestamp_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _acquire_feed_lease(feed_id: int, lease_token: str) -> bool:
    """Atomically acquire a per-feed lease, reclaiming expired leases only."""
    now = _now()
    expires_at = _timestamp_after(FEED_LEASE_SECONDS)

    with get_db() as db:
        result = db.execute(
            """
            UPDATE feeds
               SET lease_token = ?,
                   lease_expires_at = ?
             WHERE id = ?
               AND (
                    lease_token IS NULL
                    OR lease_expires_at IS NULL
                    OR lease_expires_at <= ?
               )
            """,
            (lease_token, expires_at, feed_id, now),
        )
        return result.rowcount == 1


def _renew_feed_lease(feed_id: int, lease_token: str) -> bool:
    """Extend a lease owned by this worker without taking another worker's lease."""
    with get_db() as db:
        result = db.execute(
            """
            UPDATE feeds
               SET lease_expires_at = ?
             WHERE id = ?
               AND lease_token = ?
            """,
            (_timestamp_after(FEED_LEASE_SECONDS), feed_id, lease_token),
        )
        return result.rowcount == 1


def _release_feed_lease(feed_id: int, lease_token: str) -> None:
    """Release only the lease held by this worker."""
    with get_db() as db:
        db.execute(
            """
            UPDATE feeds
               SET lease_token = NULL,
                   lease_expires_at = NULL
             WHERE id = ?
               AND lease_token = ?
            """,
            (feed_id, lease_token),
        )


def _update_last_fetched(feed_id: int, lease_token: str) -> None:
    with get_db() as db:
        db.execute(
            """
            UPDATE feeds
               SET last_fetched = ?
             WHERE id = ?
               AND lease_token = ?
            """,
            (_now(), feed_id, lease_token),
        )


def _update_cursor(feed_id: int, lease_token: str, cursor_id: str) -> bool:
    """Advance a cursor only while the current worker still owns the lease."""
    with get_db() as db:
        result = db.execute(
            """
            UPDATE feeds
               SET last_item_id = ?,
                   last_fetched = ?
             WHERE id = ?
               AND lease_token = ?
            """,
            (cursor_id, _now(), feed_id, lease_token),
        )
        return result.rowcount == 1


def check_feed(feed_id: int) -> None:
    """Fetch and deliver new feed items under an exclusive per-feed lease."""
    lease_token = secrets.token_urlsafe(32)

    if not _acquire_feed_lease(feed_id, lease_token):
        logger.info("Feed %s is already being checked; skipping", feed_id)
        return

    try:
        _check_feed_with_lease(feed_id, lease_token)
    finally:
        _release_feed_lease(feed_id, lease_token)


def _check_feed_with_lease(feed_id: int, lease_token: str) -> None:
    with get_db() as db:
        feed = db.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if not feed:
            logger.warning("Feed %s not found", feed_id)
            return

        if feed["paused"]:
            logger.info("Feed %s (%s): paused; skipping", feed_id, feed["name"])
            _update_last_fetched(feed_id, lease_token)
            return

        echoes = db.execute(
            "SELECT * FROM echoes WHERE feed_id = ? AND enabled = 1",
            (feed_id,),
        ).fetchall()

    feed_url = feed["url"]
    feed_name = feed["name"]
    last_seen_id = feed["last_item_id"]

    if not echoes:
        logger.info("Feed %s (%s): no enabled echoes", feed_id, feed_name)
        _update_last_fetched(feed_id, lease_token)
        return

    try:
        feed_data = fetch_feed(feed_url)
    except Exception:
        logger.exception("Feed %s (%s): fetch failed", feed_id, feed_name)
        _update_last_fetched(feed_id, lease_token)
        return

    items = feed_data.get("items") or []
    if not items:
        logger.info("Feed %s (%s): no items", feed_id, feed_name)
        _update_last_fetched(feed_id, lease_token)
        return

    if last_seen_id is None:
        _update_cursor(feed_id, lease_token, items[0]["id"])
        logger.info(
            "Feed %s (%s): initialized cursor to %s",
            feed_id,
            feed_name,
            items[0]["id"],
        )
        return

    new_items = get_new_items(items, last_seen_id)
    if not new_items:
        _update_last_fetched(feed_id, lease_token)
        return

    cursor_id = last_seen_id

    for item in new_items:
        if not _renew_feed_lease(feed_id, lease_token):
            logger.warning(
                "Feed %s (%s): lease was lost before processing item %s",
                feed_id,
                feed_name,
                item["id"],
            )
            return

        all_succeeded = True
        for echo in echoes:
            if not process_echo(echo, item):
                all_succeeded = False

        if not all_succeeded:
            # H-2: Never process or advance past a failed earlier item.
            logger.warning(
                "Feed %s (%s): delivery failed for item %s; stopping cursor advancement",
                feed_id,
                feed_name,
                item["id"],
            )
            break

        cursor_id = item["id"]

    if cursor_id != last_seen_id:
        if not _update_cursor(feed_id, lease_token, cursor_id):
            logger.warning("Feed %s: cursor was not updated because lease was lost", feed_id)
    else:
        _update_last_fetched(feed_id, lease_token)

    _retry_due_failures(feed_id, echoes)


def _retry_due_failures(feed_id: int, echoes) -> None:
    """Reprocess failed rows whose backoff has elapsed, regardless of cursor.

    Normal cursor replay only covers items at-or-after the cursor. Failed rows
    can sit behind it (e.g. an item that failed, was manually reset, or was
    blocked while another echo's item gated advancement). This sweep gives
    them their scheduled retries without disturbing feed ordering guarantees.
    """
    if not echoes:
        return
    echo_ids = [e["id"] for e in echoes]
    placeholders = ",".join("?" for _ in echo_ids)

    with get_db() as db:
        due = db.execute(
            f"""
            SELECT id, echo_id, item_id FROM posted_items
             WHERE echo_id IN ({placeholders})
               AND status = 'failed'
               AND next_retry_at IS NOT NULL
               AND next_retry_at <= ?
             ORDER BY id
             LIMIT 25
            """,
            (*echo_ids, _now()),
        ).fetchall()

    if not due:
        return

    # We only have item_id stored, not the full item payload. Fetch the feed
    # once and match; items that have aged out of the feed are marked gave_up.
    try:
        feed_data = fetch_feed(db_feed_url(feed_id))
    except Exception:
        logger.exception("Feed %s: fetch failed during retry sweep", feed_id)
        return

    items_by_id = {it["id"]: it for it in (feed_data.get("items") or [])}
    echoes_by_id = {e["id"]: e for e in echoes}

    for row in due:
        item = items_by_id.get(row["item_id"])
        if item is None:
            with get_db() as db:
                db.execute(
                    """UPDATE posted_items SET status = 'gave_up',
                          error_message = 'Item no longer in feed; cannot retry',
                          next_retry_at = NULL
                        WHERE id = ? AND status = 'failed'""",
                    (row["id"],),
                )
            continue
        echo = echoes_by_id.get(row["echo_id"])
        if echo is not None:
            process_echo(echo, item)


def db_feed_url(feed_id: int) -> str:
    with get_db() as db:
        row = db.execute("SELECT url FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    if not row:
        raise ValueError(f"Feed {feed_id} not found")
    return row["url"]


def _claim_post(echo_id: int, item: dict) -> tuple[int, str] | None:
    """Atomically claim an echo/item row.

    Returns ``(posted_item_id, claim_token)`` only for the worker that owns the
    pending attempt. Fresh pending rows cannot be claimed. Failed rows can be
    reclaimed once their backoff (next_retry_at) has elapsed, and pending rows
    abandoned for over ten minutes can be reclaimed.
    """
    item_id = item["id"]
    claim_token = secrets.token_urlsafe(32)
    now = _now()
    stale_before = _timestamp_after(-PENDING_RECLAIM_SECONDS)

    with get_db() as db:
        result = db.execute(
            """
            INSERT INTO posted_items (
                echo_id, item_id, item_title, item_url, status,
                claimed_at, claim_token, attempt_count, error_message
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?, 1, NULL)
            ON CONFLICT(echo_id, item_id) DO UPDATE SET
                item_title = excluded.item_title,
                item_url = excluded.item_url,
                status = 'pending',
                claimed_at = excluded.claimed_at,
                claim_token = excluded.claim_token,
                attempt_count = posted_items.attempt_count + 1,
                error_message = NULL
            WHERE posted_items.status = 'failed'
               AND (
                    posted_items.next_retry_at IS NULL
                    OR posted_items.next_retry_at <= ?
               )
               OR (
                    posted_items.status = 'pending'
                    AND (
                        posted_items.claimed_at IS NULL
                        OR posted_items.claimed_at <= ?
                    )
               )
            """,
            (
                echo_id,
                item_id,
                item.get("title", ""),
                item.get("link", ""),
                now,
                claim_token,
                now,
                stale_before,
            ),
        )

        if result.rowcount != 1:
            return None

        row = db.execute(
            """
            SELECT id, claim_token
              FROM posted_items
             WHERE echo_id = ?
               AND item_id = ?
            """,
            (echo_id, item_id),
        ).fetchone()

    if not row or row["claim_token"] != claim_token:
        return None

    return row["id"], claim_token


def _row_state(echo_id: int, item_id: str) -> str | None:
    with get_db() as db:
        row = db.execute(
            "SELECT status FROM posted_items WHERE echo_id = ? AND item_id = ?",
            (echo_id, item_id),
        ).fetchone()
    return row["status"] if row else None


def _post_succeeded(echo_id: int, item_id: str) -> bool:
    """True when the item is in a terminal state for this echo.

    'success', 'filtered', and 'gave_up' all count as handled: they must not
    be retried and must not block cursor advancement. A 'failed' row waiting
    out its retry backoff returns False here — it still gates the cursor —
    but process_echo distinguishes it from a fresh failure via _row_state so
    the run stops quietly instead of logging new failures.
    """
    return _row_state(echo_id, item_id) in ("success", "filtered", "gave_up")


def _record_filtered(echo_id: int, item: dict) -> None:
    """Record an item suppressed by the echo's keyword filter.

    Uses status 'filtered' so history shows what was dropped and the claim
    logic never retries it (only 'failed' and stale 'pending' rows are
    reclaimable).
    """
    with get_db() as db:
        db.execute(
            """
            INSERT INTO posted_items (
                echo_id, item_id, item_title, item_url, status,
                attempt_count, error_message
            )
            VALUES (?, ?, ?, ?, 'filtered', 0, NULL)
            ON CONFLICT(echo_id, item_id) DO NOTHING
            """,
            (echo_id, item["id"], item.get("title", ""), item.get("link", "")),
        )


def process_echo(echo, item: dict) -> bool:
    """Deliver one item to one echo using an atomic pending-row claim."""
    echo_id = echo["id"]
    item_id = item["id"]

    # Keyword filter: suppressed items count as handled so the cursor
    # advances and they are never delivered or retried.
    # .get-style access keeps plain-dict fixtures in tests working.
    try:
        filter_kw = echo["filter_keywords"]
        filter_mode = echo["filter_mode"]
    except (KeyError, IndexError):
        filter_kw, filter_mode = None, None
    if is_filtered(item, filter_kw, filter_mode):
        _record_filtered(echo_id, item)
        logger.info("Echo %s: item %s suppressed by keyword filter", echo_id, item_id)
        return True

    claimed = _claim_post(echo_id, item)
    if claimed is None:
        # Terminal states (success/filtered/gave_up) count as handled. A fresh
        # pending row belongs to another worker, and a failed row waiting out
        # its retry backoff is deferred — both are "not done", so the cursor
        # stays put and the run stops at this item (H-2), quietly.
        return _post_succeeded(echo_id, item_id)

    posted_id, claim_token = claimed

    try:
        content = render_template(echo["template"], item)
    except Exception:
        logger.exception("Echo %s: template render failed for item %s", echo_id, item_id)
        gave_up = _fail_post(posted_id, claim_token, echo_id, "Template rendering failed")
        return gave_up

    if not content.strip():
        gave_up = _fail_post(posted_id, claim_token, echo_id, "Rendered content was empty")
        return gave_up

    if echo["destination_type"] == "mastodon":
        return _send_mastodon(echo, item, content, echo["destination_id"], posted_id, claim_token)

    if echo["destination_type"] == "email":
        return _send_email_echo(
            echo,
            item,
            content,
            echo["destination_id"],
            posted_id,
            claim_token,
        )

    gave_up = _fail_post(
        posted_id,
        claim_token,
        echo_id,
        f"Unknown destination type: {echo['destination_type']}",
    )
    return gave_up


def _update_post(
    posted_id: int,
    claim_token: str,
    status: str,
    error: str | None = None,
) -> bool:
    """Finalize only the pending row still owned by this claim token."""
    with get_db() as db:
        result = db.execute(
            """
            UPDATE posted_items
               SET status = ?,
                   error_message = ?,
                   claimed_at = NULL,
                   claim_token = NULL,
                   posted_at = CURRENT_TIMESTAMP
             WHERE id = ?
               AND status = 'pending'
               AND claim_token = ?
            """,
            (status, error, posted_id, claim_token),
        )
        return result.rowcount == 1


def _fail_post(
    posted_id: int,
    claim_token: str,
    echo_id: int,
    error: str,
) -> bool:
    """Mark a claimed row failed, scheduling the next automatic retry.

    If the row has exhausted retry_max_attempts, it is marked 'gave_up'
    (terminal) instead, which unblocks the feed cursor. Returns the final
    status written.
    """
    cap = max_attempts()

    with get_db() as db:
        row = db.execute(
            "SELECT attempt_count FROM posted_items WHERE id = ?", (posted_id,)
        ).fetchone()
    attempts = row["attempt_count"] if row else 1

    if cap > 0 and attempts >= cap:
        final = "gave_up"
        error_out = f"Gave up after {attempts} attempts. Last error: {error}"
        retry_at = None
    else:
        final = "failed"
        error_out = error
        retry_at = next_retry_delay(attempts)

    with get_db() as db:
        db.execute(
            """
            UPDATE posted_items
               SET status = ?,
                   error_message = ?,
                   next_retry_at = ?,
                   claimed_at = NULL,
                   claim_token = NULL,
                   posted_at = CURRENT_TIMESTAMP
             WHERE id = ?
               AND status = 'pending'
               AND claim_token = ?
            """,
            (final, error_out, retry_at, posted_id, claim_token),
        )

    record_failure(echo_id)
    if final == "gave_up":
        logger.error(
            "Echo %s: item gave up after %s attempts: %s", echo_id, attempts, error
        )
    return final == "gave_up"


def _send_mastodon(
    echo,
    item: dict,
    content: str,
    account_id: int,
    posted_id: int,
    claim_token: str,
) -> bool:
    with get_db() as db:
        account = db.execute(
            "SELECT * FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()

    if not account:
        return _fail_post(
            posted_id, claim_token, echo["id"], f"Account {account_id} not found"
        )

    try:
        post_status(
            instance=account["instance"],
            access_token=account["access_token"],
            content=truncate(content, MASTODON_MAX_CHARS),
            visibility=echo["visibility"],
        )
    except Exception:
        logger.exception("Echo %s: Mastodon post failed", echo["id"])
        return _fail_post(posted_id, claim_token, echo["id"], "Mastodon delivery failed")

    ok = _update_post(posted_id, claim_token, "success")
    if ok:
        record_success(echo["id"])
    return ok


def _send_email_echo(
    echo,
    item: dict,
    content: str,
    email_account_id: int,
    posted_id: int,
    claim_token: str,
) -> bool:
    with get_db() as db:
        account = db.execute(
            "SELECT * FROM email_accounts WHERE id = ?",
            (email_account_id,),
        ).fetchone()

    if not account:
        return _fail_post(
            posted_id,
            claim_token,
            echo["id"],
            f"Email account {email_account_id} not found",
        )

    try:
        send_email(
            to_email=account["email"],
            subject=truncate(
                item.get("title") or item.get("link") or "FeedEcho: New Post",
                200,
            ),
            body=content,
        )
    except Exception:
        logger.exception("Echo %s: email delivery failed", echo["id"])
        return _fail_post(posted_id, claim_token, echo["id"], "Email delivery failed")

    ok = _update_post(posted_id, claim_token, "success")
    if ok:
        record_success(echo["id"])
    return ok


def check_all_feeds() -> None:
    with get_db() as db:
        feeds = db.execute("""
            SELECT id, name
              FROM feeds
             WHERE last_fetched IS NULL
                OR REPLACE(last_fetched, 'T', ' ') <=
                   datetime('now', '-' || poll_interval || ' minutes')
        """).fetchall()

    for feed in feeds:
        try:
            check_feed(feed["id"])
        except Exception:
            logger.exception("Error checking feed %s (%s)", feed["id"], feed["name"])


def start_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        return

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        check_all_feeds,
        trigger=IntervalTrigger(minutes=2),
        id="check_all_feeds",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    scheduler.add_job(check_all_feeds, "date", id="startup_check", replace_existing=True)


def stop_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None