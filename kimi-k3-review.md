# FeedEcho Code Review — Kimi K3 via OpenRouter

## Critical Bugs

**1. `enabled` checkbox can never disable an echo** — `app.py:246` / `templates/echoes.html:32`
An unchecked HTML checkbox is omitted from form data entirely. With `enabled: bool = Form(True)`, FastAPI falls back to the default `True` whether the box is checked or unchecked (verified with TestClient). There is no way to create a disabled echo. Use `enabled: Optional[bool] = Form(None)` and map `None -> 0`, or a hidden field pattern.

**2. `feedparser.parse_date` does not exist — date parsing is silently broken** — `feed_parser.py:140`
`feedparser` has no `parse_date` attribute (raises `AttributeError`, verified). The `except Exception` swallows it, so RSS `pubDate` strings like `"Mon, 04 Aug 2025 12:00:00 GMT"` pass through unparsed. Consequences:
- `{{ date:iso }}` and `{{ date:short }}` emit the raw RFC-822 string (verified in `template_engine._format_date`, which fails `fromisoformat` and falls back to the raw string).
- Any feed-side date logic gets garbage. Use `email.utils.parsedate_to_datetime` or `dateutil.parser`, and don't blanket-swallow.

**3. JSON Feed metadata read from the wrong level** — `feed_parser.py:104-105`
Per jsonfeed.org, `title` and `feed_url` are top-level keys. The code reads `data.get("feed", {})`, so every parsed JSON feed reports title `"Unknown"` (verified). Should be `data.get("title", ...)` / `data.get("feed_url", ...)`.

**4. `get_new_items` — missed posts and edge cases** — `feed_parser.py:112-130`
- `found_seen` is set but never used. If `last_seen_id` is not in the feed at all (rotated out, or the feed rewrote GUIDs), **every item in the feed is posted** — a spam burst. On not-found you should cap the backlog or require manual re-init.
- No dedupe *within* the fetched batch (feeds with duplicate GUIDs).
- No cap on `len(new_items)`. Combined with the not-found case, one bad fetch can fire dozens of Mastodon posts.
- `scheduler.py:95` assumes `items[0]` is newest. True for most feeds, not guaranteed; safer to track by date once #2 is fixed.

**5. No character-limit enforcement** — `scheduler.py:108-124`
`{{ content }}` can render arbitrarily long (verified: a 10k-char item renders 10k chars). Mastodon rejects >500 chars (instance-configurable). The post then fails with an opaque error logged to `posted_items`. Truncate per-account or at least warn/validate at render time.

## Security

**6. SSRF — unvalidated feed URLs and instance URLs** — `app.py:196-202`, `mastodon.py`
`POST /api/feeds` and `POST /api/accounts` accept any URL with zero validation. The server then fetches it (feed polling, `test_connection`). This is a self-hosted single-user app, so the blast radius is your own network — but it can hit `http://169.254.169.254/`, internal services, etc. At minimum: require `http(s)` scheme, resolve the host and reject private/loopback/link-local ranges. Given it's self-hosted and auth-free (below), anyone who can reach the port can use your box as a proxy scanner.

**7. No authentication on anything** — all of `app.py`
Every route, including account deletion and token-bearing test endpoints, is wide open. If this binds `0.0.0.0` (it does, `app.py:330`) and is reachable by anyone else, they own your Mastodon accounts. Fine on localhost; needs at least a shared-secret header or basic auth before any network exposure.

**8. Access tokens at rest and in logs**
- Plaintext in SQLite (`database.py:25`). Standard for this class of app, but the DB file should at least be `0600` and the threat model documented. Fernet-encrypt with a key from env if you want to do better.
- `mastodon.py:60` puts up to 200 chars of the API error response into `test_connection`'s return, and `scheduler.py:128` stores `str(e)` into `error_message`, which is rendered in history views. Mastodon errors usually don't echo the token, but httpx exception messages can include the full request URL in some paths. Low risk; worth a note.

**9. XSS — mostly safe, one gap to verify**
Jinja autoescape is on for `.html` (`app.py:31`) and feed content goes through `clean_text`/`clean_html` before rendering into posts. `clean_html` (feed_parser.py:132-137) strips only `script`/`style` — it leaves `onerror=`, `javascript:` URLs, `<img>` etc. intact. That HTML lands in `posted_items.item_title`/`item_url` and is rendered — but escaped by Jinja, so UI XSS is OK. The real risk is posting attacker-controlled HTML *to Mastodon* via `{{ content }}` — harmless (Mastodon sanitizes), but strips nothing before burning the 500-char budget on markup. Consider running `{{ content }}` through `clean_text` by default.

**10. CSRF on all state-changing POSTs** — `app.py`
All mutations are plain form POSTs with no CSRF token and no `SameSite` cookie model (there are no cookies at all). Combined with #7, any web page you visit while the app is reachable can create echoes pointing your feeds at their Mastodon account, or delete everything.

## Error handling gaps

**11. Startup crash if `static/` is missing** — `app.py:26`. `StaticFiles(directory=...)` raises at import time if the dir doesn't exist. Minor, but the app won't boot on a partial checkout.

**12. Scheduler exceptions only logged, never surfaced** — `scheduler.py:140-145`. A feed that fails every poll is invisible in the UI until the user reads logs. `feeds.last_fetched` stays stale — surface "last error" per feed in the UI.

**13. `posted_items` grows unboundedly** — `database.py:47-60`. No retention policy. Add a periodic purge (e.g. keep 90 days or N rows per echo).

**14. No uniqueness constraint** — `database.py`. Duplicate `(instance, access_token)` accounts and duplicate `(feed_id, account_id)` echoes are allowed, causing double-posts. The idempotency check in `scheduler.py:101` is per-echo, so two echoes with the same pair post twice. Add `UNIQUE(feed_id, account_id)` on echoes.

**15. Per-feed `poll_interval` is collected but ignored** — `database.py:35`, `scheduler.py:143-146`. The UI stores it, but `check_all_feeds` runs every 5 min and checks *every* feed regardless. Either schedule one job per feed keyed by `poll_interval` (and reschedule on edit), or compare `last_fetched + poll_interval <= now` in `check_all_feeds`.

## Architecture

- **Single global 5-min job with sequential fetches** — one slow feed (30s timeout) delays all others; 20 feeds × 30s worst case = 10 min inside a 5-min interval. APScheduler's default thread pool will overlap runs, and then two concurrent `check_feed` calls for the same feed can double-post (the idempotency check + `log_post` is not transactional across the two connections). Use `max_instances=1` and a lock per feed, plus `UNIQUE(echo_id, item_id)` on `posted_items` as a hard backstop.
- **Blocking I/O in async route handlers** — every route is `async def` doing synchronous sqlite3 + httpx calls. Under any concurrency this stalls the event loop. Either make routes `def` (FastAPI runs them in a threadpool) or move to `async` sqlite/httpx.
- **Per-request `PRAGMA journal_mode=WAL`** (`database.py:14`) — journal mode is persistent on the DB file; setting it per-connection is wasted work on every call.
- `@app.on_event` is deprecated in current FastAPI — use lifespan handlers.

## Missing MVP features

1. **Edit feed/account** — delete-and-recreate is the only path, and it cascades away echo history.
2. **Per-feed error visibility + "backfill N items" option** (the init flow is there, but no "post last 3" for new echoes).
3. **Retry with backoff for failed posts** — a transient Mastodon 502 is a permanent `failed` row.
4. **Content-length truncation with ellipsis** (see #5) and per-instance max char lookup (`GET /api/v1/instance`).
5. **Duplicate-feed detection by URL** on add.
6. **Conditional GET** (`ETag`/`If-Modified-Since`) — polite and cuts parse work; `last_fetched` is stored but never sent.

## Priority order

1. Fix the enabled checkbox (#1) and date parsing (#2) — both verified broken today.
2. Bound `get_new_items` on not-found (#4) — the spam-burst case is the most damaging runtime behavior.
3. Unique constraints on echoes and `posted_items(echo_id, item_id)` (#14) + scheduler concurrency guard.
4. Truncation (#5), auth story (#7/#10) before any non-localhost exposure, SSRF allowlist (#6).
5. Honor `poll_interval` (#15) or remove the field.

The bones are good — parameterized SQL everywhere, idempotency intent, clean module split. The bugs above are all small fixes; #1, #2, #3 I verified by running the code, so they're certain, not speculative.