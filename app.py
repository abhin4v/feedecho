"""FeedEcho — self-hosted RSS feed cross-poster.

Routes feed items to Mastodon accounts. Web UI for managing feeds,
accounts, echoes, and viewing post history.
"""

import os
import re
import logging
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from database import get_db, init_db
from feed_parser import fetch_feed
from mastodon import test_connection, post_status, verify_credentials
from template_engine import render_template, available_variables
from scheduler import start_scheduler, stop_scheduler, check_feed
from oauth import get_authorize_url, exchange_code, parse_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
logger = logging.getLogger("feedecho")

app = FastAPI(title="FeedEcho", version="0.1.0")

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

jinja = Environment(
    loader=FileSystemLoader(BASE_DIR / "templates"),
    autoescape=select_autoescape(["html"]),
)


def render(name: str, request: Request, status_code: int = 200, **kwargs) -> HTMLResponse:
    template = jinja.get_template(name)
    return HTMLResponse(template.render(request=request, **kwargs), status_code=status_code)


def validate_url(url: str) -> str:
    """Validate a URL has http or https scheme."""
    if not re.match(r"^https?://", url):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    return url.rstrip("/")


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    logger.info("FeedEcho started")
    yield
    stop_scheduler()


app.router.lifespan_context = lifespan


# ── Pages ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    with get_db() as db:
        accounts = db.execute("SELECT id, name, instance, created_at FROM accounts ORDER BY name").fetchall()
        feeds = db.execute("SELECT * FROM feeds ORDER BY name").fetchall()
        echoes = db.execute("""
            SELECT e.*, f.name as feed_name, f.url as feed_url,
                   a.name as account_name, a.instance as account_instance
            FROM echoes e
            JOIN feeds f ON e.feed_id = f.id
            JOIN accounts a ON e.account_id = a.id
            ORDER BY e.created_at DESC
        """).fetchall()
        recent_posts = db.execute("""
            SELECT pi.*, f.name as feed_name, a.name as account_name, a.instance as account_instance
            FROM posted_items pi
            JOIN echoes e ON pi.echo_id = e.id
            JOIN feeds f ON e.feed_id = f.id
            JOIN accounts a ON e.account_id = a.id
            ORDER BY pi.posted_at DESC
            LIMIT 20
        """).fetchall()
        stats = {
            "accounts": len(accounts),
            "feeds": len(feeds),
            "echoes": len(echoes),
            "active_echoes": sum(1 for e in echoes if e["enabled"]),
            "total_posts": db.execute("SELECT COUNT(*) FROM posted_items WHERE status = 'success'").fetchone()[0],
            "failed_posts": db.execute("SELECT COUNT(*) FROM posted_items WHERE status = 'failed'").fetchone()[0],
        }
    return render("dashboard.html", request, accounts=accounts, feeds=feeds, echoes=echoes,
                  recent_posts=recent_posts, stats=stats)


@app.get("/feeds", response_class=HTMLResponse)
async def feeds_page(request: Request):
    with get_db() as db:
        feeds = db.execute("SELECT * FROM feeds ORDER BY name").fetchall()
        feed_echoes = {}
        for f in feeds:
            feed_echoes[f["id"]] = db.execute(
                "SELECT COUNT(*) as c FROM echoes WHERE feed_id = ?", (f["id"],)
            ).fetchone()["c"]
    return render("feeds.html", request, feeds=feeds, feed_echoes=feed_echoes)


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    with get_db() as db:
        # Don't select access_token — mask it in the UI
        accounts = db.execute("SELECT id, name, instance, created_at FROM accounts ORDER BY name").fetchall()
    return render("accounts.html", request, accounts=accounts)


@app.get("/echoes", response_class=HTMLResponse)
async def echoes_page(request: Request):
    with get_db() as db:
        echoes = db.execute("""
            SELECT e.*, f.name as feed_name, a.name as account_name, a.instance
            FROM echoes e
            JOIN feeds f ON e.feed_id = f.id
            JOIN accounts a ON e.account_id = a.id
            ORDER BY e.created_at DESC
        """).fetchall()
        feeds = db.execute("SELECT * FROM feeds ORDER BY name").fetchall()
        accounts = db.execute("SELECT id, name, instance FROM accounts ORDER BY name").fetchall()
    return render("echoes.html", request, echoes=echoes, feeds=feeds, accounts=accounts,
                  template_vars=available_variables())


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    with get_db() as db:
        posts = db.execute("""
            SELECT pi.*, f.name as feed_name, a.name as account_name, a.instance
            FROM posted_items pi
            JOIN echoes e ON pi.echo_id = e.id
            JOIN feeds f ON e.feed_id = f.id
            JOIN accounts a ON e.account_id = a.id
            ORDER BY pi.posted_at DESC
            LIMIT 100
        """).fetchall()
    return render("history.html", request, posts=posts)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ── API: Accounts ───────────────────────────────────────────────────────────

@app.post("/api/accounts")
async def add_account(
    name: str = Form(...),
    instance: str = Form(...),
    access_token: str = Form(...),
):
    instance = validate_url(instance)
    with get_db() as db:
        db.execute(
            "INSERT INTO accounts (name, instance, access_token) VALUES (?, ?, ?)",
            (name, instance, access_token),
        )
    return RedirectResponse(url="/accounts", status_code=303)


@app.post("/api/accounts/{account_id}/test")
async def test_account(account_id: int):
    with get_db() as db:
        account = db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    success, message = test_connection(account["instance"], account["access_token"])
    return {"success": success, "message": message}


@app.post("/api/accounts/{account_id}/delete")
async def delete_account(account_id: int):
    with get_db() as db:
        db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return RedirectResponse(url="/accounts", status_code=303)


# ── API: Feeds ──────────────────────────────────────────────────────────────

@app.post("/api/feeds")
async def add_feed(
    name: str = Form(...),
    url: str = Form(...),
    poll_interval: int = Form(15),
):
    url = validate_url(url)
    poll_interval = max(1, min(poll_interval, 1440))
    with get_db() as db:
        db.execute(
            "INSERT INTO feeds (name, url, poll_interval) VALUES (?, ?, ?)",
            (name, url, poll_interval),
        )
    return RedirectResponse(url="/feeds", status_code=303)


@app.post("/api/feeds/{feed_id}/delete")
async def delete_feed(feed_id: int):
    with get_db() as db:
        db.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    return RedirectResponse(url="/feeds", status_code=303)


@app.post("/api/feeds/{feed_id}/test")
async def test_feed(feed_id: int):
    """Fetch a feed and return preview of items."""
    with get_db() as db:
        feed = db.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")
    try:
        feed_data = fetch_feed(feed["url"])
        preview = {
            "title": feed_data["title"],
            "type": feed_data["type"],
            "item_count": len(feed_data["items"]),
            "items": feed_data["items"][:5],
        }
        return {"success": True, "preview": preview}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/feeds/{feed_id}/init")
async def init_feed(feed_id: int):
    """Initialize a feed's last_item_id so it only posts new items going forward."""
    with get_db() as db:
        feed = db.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
        if not feed:
            raise HTTPException(status_code=404, detail="Feed not found")
        try:
            feed_data = fetch_feed(feed["url"])
            if feed_data["items"]:
                last_id = feed_data["items"][0]["id"]
                db.execute("UPDATE feeds SET last_item_id = ? WHERE id = ?", (last_id, feed_id))
                return {"success": True, "message": f"Initialized. Last item: {feed_data['items'][0]['title'][:60]}"}
            return {"success": True, "message": "Feed has no items"}
        except Exception as e:
            return {"success": False, "error": str(e)}


@app.post("/api/feeds/{feed_id}/fetch")
async def fetch_now(feed_id: int):
    """Trigger an immediate feed check."""
    try:
        check_feed(feed_id)
        return {"success": True, "message": "Feed checked"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── API: Echoes ─────────────────────────────────────────────────────────────

VALID_VISIBILITY = {"public", "unlisted", "private", "direct"}


@app.post("/api/echoes")
async def add_echo(
    feed_id: int = Form(...),
    account_id: int = Form(...),
    template: str = Form("{{ title }} {{ link }}"),
    visibility: str = Form("public"),
    enabled: str = Form(""),
):
    if visibility not in VALID_VISIBILITY:
        raise HTTPException(status_code=400, detail=f"Invalid visibility. Must be one of: {VALID_VISIBILITY}")
    # HTML checkboxes: absent = unchecked, present = checked
    is_enabled = 1 if enabled else 0
    with get_db() as db:
        db.execute(
            "INSERT INTO echoes (feed_id, account_id, template, visibility, enabled) VALUES (?, ?, ?, ?, ?)",
            (feed_id, account_id, template, visibility, is_enabled),
        )
    return RedirectResponse(url="/echoes", status_code=303)


@app.post("/api/echoes/{echo_id}/toggle")
async def toggle_echo(echo_id: int):
    with get_db() as db:
        echo = db.execute("SELECT enabled FROM echoes WHERE id = ?", (echo_id,)).fetchone()
        if not echo:
            raise HTTPException(status_code=404, detail="Echo not found")
        new_val = 0 if echo["enabled"] else 1
        db.execute("UPDATE echoes SET enabled = ? WHERE id = ?", (new_val, echo_id))
    return {"success": True, "enabled": bool(new_val)}


@app.post("/api/echoes/{echo_id}/delete")
async def delete_echo(echo_id: int):
    with get_db() as db:
        db.execute("DELETE FROM echoes WHERE id = ?", (echo_id,))
    return RedirectResponse(url="/echoes", status_code=303)


@app.post("/api/echoes/{echo_id}/template")
async def update_echo_template(
    echo_id: int,
    template: str = Form(...),
):
    with get_db() as db:
        echo = db.execute("SELECT id FROM echoes WHERE id = ?", (echo_id,)).fetchone()
        if not echo:
            raise HTTPException(status_code=404, detail="Echo not found")
        db.execute("UPDATE echoes SET template = ? WHERE id = ?", (template, echo_id))
    return RedirectResponse(url="/echoes", status_code=303)


# ── API: Preview ────────────────────────────────────────────────────────────

@app.post("/api/preview")
async def preview_template(
    template: str = Form(...),
    feed_id: int = Form(...),
):
    """Preview a template against the latest feed item."""
    with get_db() as db:
        feed = db.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found")
    try:
        feed_data = fetch_feed(feed["url"])
        if not feed_data["items"]:
            return {"success": False, "error": "Feed has no items"}
        item = feed_data["items"][0]
        rendered = render_template(template, item)
        return {"success": True, "rendered": rendered, "item_title": item["title"]}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── API: OAuth ───────────────────────────────────────────────────────────────

@app.get("/oauth/connect")
async def oauth_connect(request: Request, instance: str = ""):
    """Redirect to Mastodon OAuth authorize page."""
    if not instance:
        raise HTTPException(status_code=400, detail="Instance URL is required")
    instance = validate_url(instance)
    try:
        auth_url = get_authorize_url(instance, "")
        return RedirectResponse(url=auth_url)
    except Exception as e:
        logger.error(f"OAuth connect failed for {instance}: {e}")
        return render("accounts.html", request,
                      accounts=_get_accounts(),
                      error=f"Failed to connect to {instance}: {e}")


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle the OAuth callback from Mastodon."""
    if error:
        return render("accounts.html", request,
                      accounts=_get_accounts(),
                      error=f"Authorization denied: {error}")

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state parameter")

    try:
        _, instance = parse_state(state)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    try:
        token_data = exchange_code(instance, code)
    except Exception as e:
        logger.error(f"OAuth token exchange failed: {e}")
        return render("accounts.html", request,
                      accounts=_get_accounts(),
                      error=f"Token exchange failed: {e}")

    access_token = token_data.get("access_token")
    if not access_token:
        return render("accounts.html", request,
                      accounts=_get_accounts(),
                      error="No access token in response")

    # Verify credentials to get the account name
    try:
        creds = verify_credentials(instance, access_token)
        name = creds.get("display_name") or creds.get("username", "Unknown")
        username = creds.get("username", "unknown")
    except Exception:
        name = "Unknown"
        username = "unknown"

    with get_db() as db:
        # Use INSERT OR REPLACE so re-authenticating an existing instance updates the token
        db.execute(
            """INSERT OR REPLACE INTO accounts (name, instance, access_token)
               VALUES (?, ?, ?)""",
            (f"{name} ({username})", instance, access_token),
        )

    return RedirectResponse(url="/accounts?status=connected", status_code=303)


def _get_accounts():
    """Helper to fetch accounts without tokens."""
    with get_db() as db:
        return db.execute("SELECT id, name, instance, created_at FROM accounts ORDER BY name").fetchall()


# ── Misc ─────────────────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return render("404.html", request, status_code=404)


@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return Response(
        content="""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='14' fill='#2d5a9e'/><path d='M32 16 L48 32 L32 48 L16 32 Z' fill='#fff'/><circle cx='32' cy='32' r='6' fill='#2d5a9e'/></svg>""",
        media_type="image/svg+xml",
    )


if __name__ == "__main__":
    import uvicorn
    # Bind to localhost — use a reverse proxy for remote access
    uvicorn.run(app, host="127.0.0.1", port=8453)
