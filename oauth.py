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
import hmac
import hashlib
import os
import secrets
from database import get_db

# The public URL that Mastodon will redirect back to.
# Configurable via FEEDCHO_CALLBACK_URL env var so self-hosters don't edit source.
CALLBACK_URL = os.environ.get(
    "FEEDCHO_CALLBACK_URL",
    "https://feedecho.snakepit.us/oauth/callback",
)
SCOPES = "read write"

# Secret key for HMAC-signing OAuth state tokens. Uses the same env var as
# the auth middleware if set, falls back to a per-process random key (state
# tokens won't survive a restart, but OAuth flows complete within seconds).
_STATE_SECRET = os.environ.get(
    "FEEDCHO_AUTH_TOKEN",
    os.environ.get("FEEDCHO_STATE_SECRET", secrets.token_urlsafe(32)),
).encode()


def _sign_state(instance: str) -> str:
    """Create an HMAC-signed state token for the given instance.

    Format: <random_nonce>|<instance>|<hmac>
    Uses | as delimiter because instance URLs contain : (https://).
    The HMAC covers nonce + instance, preventing tampering with the
    instance field or forging a valid state without the secret.
    """
    nonce = secrets.token_urlsafe(16)
    payload = f"{nonce}|{instance}"
    sig = hmac.new(_STATE_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload}|{sig}"


def _verify_state(state: str) -> str:
    """Verify an HMAC-signed state token and return the instance.

    Raises ValueError if the signature is invalid or the token is malformed.
    """
    parts = state.rsplit("|", 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid state parameter: {state}")
    nonce, instance, sig = parts
    payload = f"{nonce}|{instance}"
    expected_sig = hmac.new(_STATE_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected_sig):
        raise ValueError("Invalid state signature")
    return instance


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


def get_authorize_url(instance: str) -> str:
    """Build the OAuth authorize URL for the instance.

    The state parameter is HMAC-signed to prevent CSRF and tampering —
    we embed the instance URL so we know which instance to call back to,
    and the signature prevents an attacker from forging a state token.
    """
    instance = instance.rstrip("/")
    app = get_or_create_app(instance)
    state_token = _sign_state(instance)
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


def verify_state(state: str) -> str:
    """Verify an HMAC-signed state token and return the instance.

    Raises ValueError if the signature is invalid or the token is malformed.
    """
    return _verify_state(state)
