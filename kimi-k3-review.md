# FeedEcho Code Review

Overall: clean structure, parameterized SQL throughout (no injection issues found), and good instincts on idempotency and first-run backlog suppression. The findings below are ordered roughly by severity within each category.

---

## 1. Bugs and Edge Cases

### Critical

**`feedparser.parse_date` does not exist (`feed_parser.py:parse_date`).** Every call raises `AttributeError`, which is swallowed by the bare `except Exception`, so date parsing silently never works and raw strings pass through. Worse, you're re-parsing strings when feedparser already did the work: use `entry.get("published_parsed") or entry.get("updated_parsed")` (a `time.struct_time`) and convert with `datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)`. This fixes RFC-822 RSS dates, ISO Atom dates, and removes the dependency on string parsing entirely.

**JSON Feed parsing doesn't follow the spec (`feed_parser.py:parse_json_feed`).** `data.get("feed", {})` — JSON Feed has no top-level `feed` key, so every JSON feed's title is "Unknown". Use `data.get("title", ...)`. Also:
- `entry.get("author", {}).get("name", "")` crashes with `AttributeError` when `author` is explicitly `null` (valid JSON). Use `(entry.get("author") or {})`.
- JSON Feed 1.1 uses `authors: [{name, url}]` (array); `author` is 1.0-only. Handle both.

**First-run feeds never post (`scheduler.py:check_feed`).** When `last_item_id` is NULL, `get_new_items` returns `[]`, and the "no new items" branch updates only `last_fetched` — `last_item_id` stays NULL forever. The feed will silently never post unless the user manually hits `/api/feeds/{id}/init`. Auto-initialize instead:

```python
if last_seen_id is None:
    db.execute("UPDATE feeds SET last_item_id = ?, last_fetched = ? WHERE id = ?",
               (items[0]["id"], now_iso(), feed_id))
    return
```

**`found_seen` is set but never used (`feed_parser.py:get_new_items`).** If `last_seen_id` has scrolled out of the feed (or IDs changed), *every* item is treated as new and posted. The per-echo idempotency check partially masks this for existing echoes, but a newly created echo on such a feed will spam the entire backlog to Mastodon. Fix: when `found_seen` is False, cap posts (e.g., newest item only, or newest N=3) and log a warning.

**`poll_interval` is dead config (`scheduler.py`).** The column exists, the UI accepts it, but `check_all_feeds` checks every feed every 5 minutes regardless. Either enforce it (`WHERE last_fetched IS NULL OR last_fetched <= datetime('now', '-' || poll_interval || ' minutes')`) or remove the field — as-is it's a false promise.

**Failed posts are permanently skipped.** Two compounding problems in `scheduler.py`:
1. The idempotency check matches rows with `status='failed'`, so a failed post is never retried.
2. `last_item_id` advances to `items[0]` even if every post failed, so the items are never seen as "new" again.

Result: a transient Mastodon outage means silent, permanent post loss. Filter the idempotency check to `status='success'`, and either add a retry pass over failed rows or only advance the cursor past successfully processed items.

**Non-atomic external side effects inside one DB transaction.** `check_feed` holds a single transaction across feed fetch + all Mastodon posts. If the process dies mid-loop (or any unexpected exception escapes), the transaction rolls back — posts already made to Mastodon have no DB record, and the next poll reposts them (duplicates). Commit per item: advance `last_item_id` after each item is processed, and write `posted_items` rows in their own transactions. This also fixes the next issue.

### High

**Write lock held during network I/O.** That same long transaction blocks web UI writes for the duration of up to 30s of fetching plus N sequential Mastodon posts. With WAL, readers are fine, but writers will hit `database is locked` after SQLite's default 5s timeout. Fetch first, then write; keep transactions to pure DB work. Also set `sqlite3.connect(..., timeout=30)` / `PRAGMA busy_timeout` as a safety net.

**`{{ content }}` posts raw HTML to Mastodon (`template_engine.py` + `feed_parser.py:clean_html`).** `clean_html` strips only script/style; Mastodon's `status` field is plain text, so users get literal `<p>` tags in their timeline. For Mastodon output, HTML should be *stripped* (with entity decoding — `html.unescape`), not sanitized-for-display. Also: no 500-character truncation, so long-content posts fail with 422.

**Items with empty IDs break dedup entirely.** `id` falls back to `entry.get("link", "")`; feeds without guid/link produce `id=""` for all items. Since `"" == ""`, the first item matches `last_seen_id` and *nothing ever posts*. Synthesize an ID: `hashlib.sha256((title + link + date).encode()).hexdigest()[:16]` when none exists.

### Medium

- **Newest-first ordering assumed everywhere.** `get_new_items` and `items[0]` as "newest" break on oldest-first feeds (they exist). If dates are available, sort by date descending before processing.
- **Checkbox bug:** `enabled: bool = Form(True)` means an unchecked HTML checkbox (field absent) defaults to `True` — you cannot create a disabled echo via a normal form.
- **Duplicate echoes double-post.** No `UNIQUE(feed_id, account_id)` constraint; two identical echo rows post the same item twice to the same account (idempotency is per `echo_id`).
- **Timestamps in two formats:** DB defaults write `CURRENT_TIMESTAMP` (`YYYY-MM-DD HH:MM:SS`), code writes `isoformat()` with `+00:00`. Pick one (ISO-8601 UTC) or sorting/comparison bugs will creep in.
- **`clean_text` regex** doesn't decode entities (`&amp;` shows in titles) and breaks on `>` inside attributes. feedparser already sanitizes titles reasonably; at minimum add `html.unescape`.
- Import-time `init_db()` in `database.py` runs on any import (including tests) and again at startup — move to startup only.

---

## 2. Security

**No authentication — the biggest issue.** The app binds `0.0.0.0:8453` with zero auth. Anyone who reaches the port can delete all your data, add their own feeds, and — critically — *post arbitrary content through your stored Mastodon tokens* by creating an echo pointing at a feed they control. For a self-hosted app, minimum viable options: a single bearer token / basic auth from an env var, or bind to localhost with documented reverse-proxy auth. This must be addressed before the MVP is usable.

**SSRF with an exfiltration channel.** `add_feed` accepts any URL, `fetch_feed` follows redirects, and `/api/feeds/{id}/test` *returns parsed content to the caller*. Point it at `http://169.254.169.254/latest/meta-data/` or an internal service and read the response via the test endpoint. Fixes:
- Validate scheme is `http`/`https` at input time.
- Optionally block RFC-1918/loopback/link-local resolution (with an env-var escape hatch, since self-hosters legitimately follow internal feeds).
- Enforce a response size cap (stream the body, abort at ~10 MB) — currently a hostile "feed" can OOM the process, and it also bounds decompression-bomb and parser-DoS risk.

**Drive-by CSRF works even without app auth.** If a user runs this on localhost, any website they visit can POST forms to `http://localhost:8453/api/feeds` (adding SSRF feeds, deleting accounts). SameSite cookies don't help since there's no session; a simple check (e.g., require a custom header via fetch, or verify `Origin`/`Host`) closes this.

**Access tokens in plaintext, overexposed.** `access_token` is stored unencrypted and `SELECT * FROM accounts` passes the full row — token included — into Jinja templates on the accounts page and dashboard. Mask it in UI queries (`SELECT id, name, instance, ...`), and consider Fernet encryption with a key from env (or at minimum: document the threat model and `chmod 600` the DB). Also note `test_connection` echoes `e.response.text[:200]` to the UI — instance error pages can leak internal details.

**XSS risk via preview/history.** Autoescape is on (good — assuming templates don't use `|safe`), but `{{ content }}` and the `/api/preview` response carry feed-controlled HTML. If the frontend injects preview output via `innerHTML`, that's reflected XSS from any feed you follow. Ensure the UI uses `textContent` for all rendered output, and replace regex-based `clean_html` with a real sanitizer (`nh3`/`bleach`) if HTML is ever displayed.

**SQL injection: clean.** All queries parameterized, `ORDER BY` clauses are static. Keep it that way if you add sorting later.

---

## 3. Error Handling Gaps

- **No retry/backoff for failing feeds or posts.** A dead feed costs a 30s timeout every cycle, serialized with all other feeds — enough dead feeds and `check_all_feeds` overruns its own interval. Track `consecutive_failures` per feed, back off exponentially, and surface the last error on the dashboard.
- **No Mastodon rate-limit handling.** 429s (with `Retry-After`) and 5xx should be retried; Mastodon also supports an `Idempotency-Key` header — send a hash of `echo_id + item_id` to make retries safe.
- **No input validation:** feed URL and instance URL aren't validated at creation (a scheme-less `instance` fails only later, at post time); `visibility` accepts arbitrary strings (Mastodon 422s on bad values); `poll_interval` accepts 0/negatives.
- **Broad `except Exception`** in scheduler and API routes discards error taxonomy — distinguish fetch errors (retryable) from parse errors (feed is broken) from auth errors (token revoked; disable echo and notify).
- No global 500 handler; unhandled route exceptions (e.g., `database is locked`) dump FastAPI's default error.
- `update_echo_template` and the delete endpoints silently succeed on nonexistent IDs.
- The scheduler's first run is 5 minutes after startup with no feedback; run `check_all_feeds` once at startup (or at least on feed creation).

---

## 4. Architecture Improvements

1. **Per-feed APScheduler jobs** (or a thread pool in `check_all_feeds`) instead of one serial loop — respects `poll_interval`, isolates failures, and parallelizes I/O.
2. **Separate fetch from write.** Restructure `check_feed`: fetch + parse (no DB held) → open short transaction → idempotency check + insert + cursor advance per item → close. This solves lock contention, atomicity, and retry semantics in one move.
3. **Type the feed item.** A `@dataclass`/`pydantic.BaseModel` `FeedItem` instead of dicts threaded through parser → template → scheduler would have caught several of the `.get()` fragility issues above at definition time.
4. **Config via `pydantic-settings`:** host/port, auth token, DB path, user agent, max feed size, worker guard — instead of scattered env reads and hardcoded values.
5. **Modernize FastAPI lifecycle:** `@app.on_event` is deprecated → use `lifespan`. Also note the scheduler breaks under `uvicorn --workers N` (N duplicate schedulers → duplicate posts; the check-then-post race makes the idempotency check insufficient). Document single-worker requirement or add a file-lock guard.
6. **Schema migrations:** use `PRAGMA user_version` + migration steps now, while the schema is small, and add the missing constraints (`UNIQUE(feed_id, account_id)`, `UNIQUE(posted_items.echo_id, posted_items.item_id)` as a hard backstop).
7. **Shared `httpx.Client`** at module level with configured limits/timeouts rather than per-call clients.
8. **Tests** — the highest-value targets: `get_new_items` state transitions (missing cursor, scrolled-off cursor, empty IDs), parser fixtures for RSS/Atom/JSON, template rendering, and scheduler behavior with `respx`-mocked HTTP.

---

## 5. Missing for MVP

- **Authentication** (see Security — non-negotiable).
- **Retry/manual repost** for failed items (the history page shows failures with no recourse).
- **Post-length handling:** truncate with ellipsis, and optionally query the instance's `max_toot_chars` from `/api/v2/instance`.
- **Conditional GET** (ETag/Last-Modified per feed) — bandwidth and politeness; feedparser makes this easy.
- **Feed health surfacing:** last error + consecutive failure count on the feeds page; auto-disable after N failures.
- **`/healthz`** for Docker/uptime monitoring.
- **Token masking** in the accounts UI.
- **Content Warning support** (Mastodon `spoiler_text`) — cross-posters without CW support are a common source of moderation complaints.
- **Per-echo filters** (include/exclude keywords) — the most-requested cross-poster feature after basics work.
- **Deployment artifacts:** Dockerfile/compose example and README guidance on binding, reverse proxy, and token security.

---

### Top 5 fixes if you do nothing else

1. Add authentication (or bind localhost-only) — currently anyone can post through your accounts.
2. Fix `feedparser.parse_date` (use `*_parsed` struct_time fields) and the JSON Feed `feed` key.
3. Auto-initialize `last_item_id` on first successful fetch — feeds currently never post without manual init.
4. Handle `found_seen=False` and make failed posts retryable — the two silent data-loss/spam paths in state tracking.
5. Restructure `check_feed` so network I/O happens outside DB transactions with per-item commits.
