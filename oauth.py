"""Mastodon OAuth — register app, authorize, exchange code for token.

Flow:
1. User enters instance URL
2. We register an OAuth app on that instance → get client_id/secret
3. Redirect user to instance OAuth authorize page
4. User authorizes → Mastodon redirects back with ?code=XXX
5. We exchange code for access token
6. Store the account with the token
"""

import httpx
import secrets
from database import get_db

# The public URL that Mastodon will redirect back to
CALLBACK_URL = "https://feedecho.snakepit.us/oauth/callback"
SCOPES = "read write"


def get_or_create_app(instance: str) -> dict:
    """Register an OAuth app on the instance, or return cached credentials.

    Returns: {client_id, client_secret}
    """
    instance = instance.rstrip("/")

    # Check cache first
    with get_db() as db:
        row = db.execute(
            "SELECT client_id, client_secret FROM oauth_apps WHERE instance = ?",
            (instance,),
        ).fetchone()
        if row:
            return {"client_id": row["client_id"], "client_secret": row["client_secret"]}

    # Register a new app
    url = f"{instance}/api/v1/apps"
    data = {
        "client_name": "FeedEcho",
        "redirect_uris": CALLBACK_URL,
        "scopes": SCOPES,
        "website": "https://feedecho.snakepit.us",
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, data=data)
        resp.raise_for_status()
        result = resp.json()

    # Cache it
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO oauth_apps (instance, client_id, client_secret) VALUES (?, ?, ?)",
            (instance, result["client_id"], result["client_secret"]),
        )

    return {"client_id": result["client_id"], "client_secret": result["client_secret"]}


def get_authorize_url(instance: str, state: str) -> str:
    """Build the OAuth authorize URL for the instance.

    The state parameter is used to prevent CSRF — we pass the instance
    through it so we know which instance to call back to.
    """
    instance = instance.rstrip("/")
    app = get_or_create_app(instance)
    # state format: random_token:instance
    state_token = f"{secrets.token_urlsafe(16)}:{instance}"
    return (
        f"{instance}/oauth/authorize"
        f"?client_id={app['client_id']}"
        f"&redirect_uri={CALLBACK_URL}"
        f"&response_type=code"
        f"&scope={SCOPES.replace(' ', '+')}"
        f"&state={state_token}"
    )


def exchange_code(instance: str, code: str) -> dict:
    """Exchange the OAuth callback code for an access token.

    Returns: {access_token, scope, account_id (Mastodon account ID)}
    """
    instance = instance.rstrip("/")
    app = get_or_create_app(instance)
    url = f"{instance}/oauth/token"
    data = {
        "client_id": app["client_id"],
        "client_secret": app["client_secret"],
        "redirect_uri": CALLBACK_URL,
        "grant_type": "authorization_code",
        "code": code,
        "scope": SCOPES,
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, data=data)
        resp.raise_for_status()
        return resp.json()


def parse_state(state: str) -> tuple[str, str]:
    """Parse the state parameter back into (token, instance).

    Returns (token, instance) or raises ValueError.
    """
    if ":" not in state:
        raise ValueError(f"Invalid state parameter: {state}")
    parts = state.split(":", 1)
    return parts[0], parts[1]
