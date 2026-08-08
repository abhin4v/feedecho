# FeedEcho

Self-hosted RSS feed cross-poster. Route items from RSS, Atom, and JSON feeds to Mastodon accounts using configurable templates.

Built as a replacement for [Echofeed](https://rknight.me/blog/shutting-down-echofeed/), which shut down in August 2026.

## Features

- **RSS/Atom/JSON feed support** via feedparser
- **Mastodon OAuth** — connect accounts with one click, no manual token creation
- **Template engine** with variables: `{{ title }}`, `{{ link }}`, `{{ summary }}`, `{{ content }}`, `{{ author }}`, `{{ date }}`, `{{ date:iso }}`, `{{ date:short }}`, `{{ hashtags }}`
- **Multiple accounts** — post to multiple Mastodon instances
- **Per-feed poll intervals** — each feed checked on its own schedule
- **Post history** with success/failure tracking and error messages
- **Visibility settings** — public, unlisted, private, direct
- **Mobile-responsive** — tables convert to cards, forms stack, 44px touch targets
- **Idempotent posting** — failed posts are retried, duplicates are prevented
- **Auto-initialization** — feeds set their baseline on first fetch, no manual init needed
- **Email destination** — echo to email via SMTP in addition to Mastodon

## Tech Stack

- **Backend**: Python + FastAPI
- **Database**: SQLite (WAL mode)
- **Frontend**: Jinja2 server-rendered templates + vanilla JS
- **Feed parsing**: feedparser (RSS/Atom) + native JSON Feed parser
- **Scheduler**: APScheduler (background feed checker)
- **HTTP client**: httpx

## Quick Start

```bash
git clone https://github.com/jcrabapple/feedecho.git
cd feedecho
python -m venv .venv
source .venv/bin/activate
pip install fastapi "uvicorn[standard]" jinja2 python-multipart feedparser httpx apscheduler
python -m uvicorn app:app --host 0.0.0.0 --port 8453
```

Open `http://localhost:8453` in your browser.

## Usage

1. **Add a Mastodon account** — Go to `/accounts`, enter your instance URL, click "Connect Account". OAuth handles the rest.
2. **Add a feed** — Go to `/feeds`, paste an RSS/Atom/JSON feed URL.
3. **Create an echo** — Go to `/echoes`, select a feed + account, write a template like `{{ title }} {{ link }}`.
4. **Watch it run** — The scheduler checks feeds every 2 minutes and posts new items.

## Template Variables

| Variable | Description |
|----------|-------------|
| `{{ title }}` | Post title |
| `{{ link }}` | Post URL |
| `{{ summary }}` | Post summary/excerpt |
| `{{ content }}` | Full post content (HTML stripped to plain text) |
| `{{ author }}` | Author name |
| `{{ date }}` | Publication date (raw) |
| `{{ date:iso }}` | ISO 8601 date (2024-01-15T09:30:00) |
| `{{ date:short }}` | Short date (2024-01-15) |
| `{{ hashtags }}` | Feed tags as #hashtags |

## Code Breakdown

FeedEcho is ~1,600 lines of Python across 8 modules. No framework magic, no ORMs, no build step. Here's what each piece does:

### `app.py` (613 lines) — Web server and routes

The FastAPI application. Defines every HTTP route: dashboard, feed CRUD, account management, echo CRUD, post history, settings, and the OAuth callback endpoints. Renders Jinja2 templates server-side. Also starts/stops the background scheduler on app startup/shutdown. This is the only module that talks to the user's browser.

### `database.py` (124 lines) — SQLite layer

Creates and manages 7 tables: `accounts` (Mastodon connections), `feeds` (RSS sources), `echoes` (feed-to-destination mappings), `email_accounts`, `settings` (key-value config like SMTP), `posted_items` (post history with status tracking), and `oauth_apps` (cached OAuth client credentials per instance). Uses SQLite WAL mode for concurrent read/write. Includes lightweight migrations (column additions, schema resets) so existing databases upgrade in place. The unique index on `posted_items(echo_id, item_id)` enforces the pending-row dedup pattern.

### `feed_parser.py` (212 lines) — Feed fetching and normalization

Fetches feed URLs via httpx with a 10 MB size cap (prevents OOM from hostile feeds). Parses RSS/Atom via feedparser and JSON Feed natively. Normalizes all feed formats into a common item shape: `id`, `title`, `link`, `summary`, `content`, `author`, `date`, `tags`. Strips HTML to plain text (Mastodon statuses are plain text). Synthesizes stable item IDs from content hashes when feeds lack GUIDs. The `get_new_items()` function implements cursor-based new-item detection: on first run it sets a baseline (no backlog posting), and if the cursor scrolled off the feed, it posts only the newest item to avoid spam.

### `scheduler.py` (302 lines) — Background feed checker

The core dispatch engine. Runs on APScheduler (every 2 minutes). For each due feed: fetch new items, find enabled echoes, render templates, dispatch to Mastodon or email. Uses a **pending-row pattern** for idempotent posting: each (echo, item) pair is claimed via `INSERT OR IGNORE` with `status='pending'` before dispatch, then `UPDATE`d to `success` or `failed` after. The unique index prevents duplicate claims. The cursor only advances past items where all echoes succeeded, so failed posts are retried on the next poll. All network I/O happens outside DB transactions to avoid lock contention.

### `mastodon.py` (67 lines) — Mastodon API client

Thin httpx wrapper around three Mastodon REST endpoints: `POST /api/v1/statuses` (post), `GET /api/v1/accounts/verify_credentials` (validate token), and the connection test helper. No state, no caching, no surprises.

### `oauth.py` (110 lines) — Mastodon OAuth 2.0 flow

Implements the full OAuth dance: register an app on the target instance (`POST /api/v1/apps`), build the authorize URL with a CSRF state token, exchange the callback code for an access token (`POST /oauth/token`). Caches OAuth app credentials per instance in the `oauth_apps` table so re-registration isn't needed. The state parameter carries a random token plus the instance URL so the callback knows which instance to exchange with.

### `template_engine.py` (76 lines) — Template rendering

Regex-based variable substitution. Replaces `{{ variable }}` placeholders with feed item data. Supports 9 variables including two date format variants. No eval, no code execution — pure string replacement. Tags are sanitized to alphanumeric for hashtag safety.

### `email_sender.py` (99 lines) — SMTP email dispatch

Sends rendered template content as plain-text email. Reads SMTP config (host, port, username, password, TLS mode) from the `settings` table. Supports both implicit TLS (port 465) and STARTTLS (port 587). Includes a connection test helper.

### `templates/` — Jinja2 HTML templates

8 templates: `base.html` (layout + nav), `dashboard.html` (overview stats), `feeds.html`, `accounts.html`, `echoes.html`, `history.html` (post log), `settings.html` (SMTP config), `404.html`. All use Jinja2 autoescaping.

### `static/` — CSS and JavaScript

`style.css` (mobile-responsive, table-to-card at 640px breakpoint) and `app.js` (inline echo editing, account test buttons, feed preview). Vanilla JS, no frameworks, no build step.

### `tests/` — 45 pytest tests

Three test modules covering the database layer, feed parser (item detection, HTML stripping, truncation, date parsing), and template engine (variable substitution, date formatting, hashtag generation).

## Security

FeedEcho handles OAuth tokens and posts to your Mastodon accounts. Here's what it does and doesn't do:

### Secrets handling

- **Mastodon OAuth tokens** are stored in the SQLite database (`accounts.access_token`). The database file is local to the server. There is no encryption at rest — if an attacker gains filesystem access, they can read the tokens. This is the same trust model as any self-hosted app with a local database.
- **SMTP passwords** are stored in the `settings` table in plaintext. Same caveat applies.
- **OAuth client secrets** (per-instance app credentials) are cached in the `oauth_apps` table. These are less sensitive than user tokens but are stored in plaintext.
- FeedEcho **does not** log tokens, passwords, or secrets to the application log. Log messages contain echo IDs, feed names, and error messages only.

### OAuth flow

- The OAuth state parameter includes a cryptographically random token (`secrets.token_urlsafe(16)`) to prevent CSRF during the authorization flow.
- The callback URL is hardcoded to the deployment's public URL. If you self-host, update `CALLBACK_URL` in `oauth.py` to match your domain.

### Input handling

- Feed content from external RSS/Atom/JSON feeds is treated as untrusted. HTML is stripped to plain text before posting to Mastodon (Mastodon statuses are plain text). Feed item titles and URLs are never rendered as HTML in the UI without Jinja2 autoescaping.
- Template variables are substituted via regex — there is no `eval()` or code execution path. A malformed template produces empty or garbled output, not a security hole.
- Feed fetches are capped at 10 MB to prevent memory exhaustion from hostile feeds.
- The inline echo editor in `app.js` stores original row HTML in an in-memory Map rather than serializing it into a DOM attribute, avoiding an XSS vector that was present in an earlier version.

### Network

- All outbound HTTP uses httpx with a 30-second timeout. FeedEcho makes requests to: the feed URL (user-provided), the Mastodon instance API (user-provided), and the SMTP server (admin-configured). No telemetry, no phone-home, no analytics.
- There is no authentication on the web UI. FeedEcho is designed to run behind a reverse proxy or tunnel (Cloudflare Tunnel, nginx, etc.) with access control at the network layer. If you expose the port directly to the internet, anyone who can reach it can manage your feeds and post to your accounts.

### What FeedEcho does NOT do

- Does not encrypt secrets at rest
- Does not require authentication on the web UI
- Does not rate-limit its own feed polling (relies on APScheduler intervals)
- Does not validate SSL certificates beyond httpx defaults
- Does not sandbox feed parsing (feedparser runs in-process)

If any of these are a concern for your deployment, wrap FeedEcho behind an authenticated reverse proxy and restrict filesystem access to the database file.

## Deployment

### systemd user service

```ini
[Unit]
Description=FeedEcho
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/projects/feedecho
ExecStart=%h/projects/feedecho/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8453
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

### Cloudflare Tunnel

For public access with HTTPS, use a Cloudflare Tunnel:

```bash
cloudflared tunnel create feedecho
cloudflared tunnel route dns <TUNNEL_ID> feedecho.yourdomain.com

cat > ~/.cloudflared/feedecho.yml << 'EOF'
tunnel: feedecho
credentials-file: /home/jason/.cloudflared/<TUNNEL_ID>.json

ingress:
  - hostname: feedecho.yourdomain.com
    service: http://127.0.0.1:8453
  - service: http_status:404
EOF
```

If using OAuth, update `CALLBACK_URL` in `oauth.py` to match your public URL.

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## Project Structure

```
feedecho/
├── app.py              # FastAPI app — routes, templates, OAuth callbacks
├── database.py         # SQLite layer (7 tables)
├── feed_parser.py     # RSS/Atom/JSON feed fetching + parsing
├── mastodon.py        # Mastodon API client
├── oauth.py           # Mastodon OAuth 2.0 flow
├── scheduler.py       # APScheduler background feed checker
├── template_engine.py # Variable substitution
├── email_sender.py    # SMTP email dispatch
├── templates/         # Jinja2 HTML templates
├── static/            # CSS + JS
└── tests/             # 45 tests (pytest)
```

## License

MIT
