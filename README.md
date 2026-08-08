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

## Project Structure

```
feedecho/
├── app.py              # FastAPI app — routes, templates, OAuth callbacks
├── database.py         # SQLite layer (5 tables)
├── feed_parser.py     # RSS/Atom/JSON feed fetching + parsing
├── mastodon.py        # Mastodon API client
├── oauth.py           # Mastodon OAuth 2.0 flow
├── scheduler.py       # APScheduler background feed checker
├── template_engine.py # Variable substitution
├── templates/         # Jinja2 HTML templates
├── static/            # CSS + JS
└── tests/             # 45 tests (pytest)
```

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## License

MIT
