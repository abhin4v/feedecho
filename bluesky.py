"""Bluesky (AT Protocol) client — sessions, PDS resolution, posts, and images.

FeedEcho connects to Bluesky using app passwords (the standard method for
bots and automation). App passwords are created in the Bluesky app under
Settings > Privacy & Security > App Passwords, and can only create posts —
they cannot change account settings or be used to log in to the app.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from feed_parser import validate_outbound_url

PUBLIC_API = "https://public.api.bsky.app"
PLC_DIRECTORY = "https://plc.directory"
POST_COLLECTION = "app.bsky.feed.post"

MAX_POST_GRAPHEMES = 300
MAX_ALT_GRAPHEMES = 1000
MAX_BLOB_BYTES = 1_000_000  # bsky.social PDS limit for image blobs
BLUESKY_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# How long to trust a cached access JWT before refreshing (JWT exp takes
# precedence when decodable). Access tokens typically live ~2 hours.
DEFAULT_SESSION_TTL_SECONDS = 2 * 60 * 60


class BlueskyError(Exception):
    """Base error for Bluesky API interactions."""


class BlueskyAuthError(BlueskyError):
    """Credentials rejected, session expired, or app password revoked."""


# ── Handle normalization ─────────────────────────────────────────────────────


def normalize_handle(raw: str) -> str:
    """Normalize user-entered handles to lowercase bare form.

    Accepts "Name", "@Name.Bsky.Social", and "https://bsky.app/profile/x"
    style inputs (the host part is taken from URLs). Raises ValueError when
    nothing handle-like remains.
    """
    value = (raw or "").strip().lower().removeprefix("@")
    value = re.sub(r"^https?://", "", value)
    # Take the last path segment if given a profile URL.
    value = value.split("/")[-1].strip()
    value = value.rstrip(".").strip()
    if not value or "." not in value or " " in value:
        raise ValueError("Enter a valid Bluesky handle, e.g. username.bsky.social")
    return value


# ── PDS discovery ────────────────────────────────────────────────────────────


def resolve_pds(handle: str) -> tuple[str, str]:
    """Resolve a handle to (did, pds_url) via the public identity service.

    Handles hosted on bsky.social and custom PDSes both work: the DID
    document's #atproto_pds service entry tells us where the account's data
    lives, so we never hardcode bsky.social.
    """
    handle = normalize_handle(handle)
    validate_outbound_url(PUBLIC_API)

    try:
        with httpx.Client(
            timeout=httpx.Timeout(15.0), follow_redirects=False
        ) as client:
            response = client.get(
                f"{PUBLIC_API}/xrpc/com.atproto.identity.resolveHandle",
                params={"handle": handle},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BlueskyError(f"Could not resolve handle '{handle}'") from exc

    did = data.get("did")
    if not isinstance(did, str) or not did:
        raise BlueskyError(f"Handle '{handle}' did not resolve to a DID")

    if did.startswith("did:web:"):
        # did:web:example.com -> https://example.com/.well-known/did.json
        pds = f"https://{did.removeprefix('did:web:')}"
    else:
        # did:plc:... -> query the PLC directory for the PDS service endpoint.
        try:
            with httpx.Client(
                timeout=httpx.Timeout(15.0), follow_redirects=False
            ) as client:
                response = client.get(f"{PLC_DIRECTORY}/{quote(did, safe='')}")
                response.raise_for_status()
                doc = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BlueskyError(f"Could not fetch DID document for '{handle}'") from exc

        pds = ""
        for service in doc.get("service") or []:
            if service.get("id") == "#atproto_pds" and service.get("serviceEndpoint"):
                pds = service["serviceEndpoint"]
                break
        if not pds:
            raise BlueskyError(f"No PDS endpoint found for '{handle}'")

    pds = pds.rstrip("/")
    # The PDS hostname came from a remote identity document — validate it
    # against SSRF/private-IP rules before making requests to it.
    validate_outbound_url(pds)
    return did, pds


# ── Sessions ─────────────────────────────────────────────────────────────────


def _decode_jwt_exp(jwt_str: str) -> datetime | None:
    """Return the exp claim as an aware UTC datetime, or None if undecodable."""
    try:
        payload = jwt_str.split(".")[1]
        # base64url decode with padding restored
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
        if isinstance(exp, (int, float)):
            return datetime.fromtimestamp(exp, tz=timezone.utc)
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def create_session(pds: str, handle: str, app_password: str) -> dict:
    """Create a session with an app password. Returns did + JWTs."""
    pds = pds.rstrip("/")
    validate_outbound_url(pds)

    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0), follow_redirects=False
        ) as client:
            response = client.post(
                f"{pds}/xrpc/com.atproto.server.createSession",
                json={"identifier": handle, "password": app_password},
            )
    except httpx.RequestError as exc:
        raise BlueskyError(f"Could not reach PDS {pds}") from exc

    if response.status_code in (400, 401):
        raise BlueskyAuthError("Invalid handle or app password")
    if response.status_code >= 400:
        raise BlueskyError(f"PDS returned HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise BlueskyError("PDS returned an invalid session response") from exc

    did = data.get("did")
    access_jwt = data.get("accessJwt")
    refresh_jwt = data.get("refreshJwt")
    if not all(isinstance(v, str) and v for v in (did, access_jwt, refresh_jwt)):
        raise BlueskyError("PDS returned an incomplete session response")
    return {"did": did, "access_jwt": access_jwt, "refresh_jwt": refresh_jwt}


def refresh_session(pds: str, refresh_jwt: str) -> dict:
    """Refresh a session using its refresh JWT."""
    pds = pds.rstrip("/")
    validate_outbound_url(pds)

    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0), follow_redirects=False
        ) as client:
            response = client.post(
                f"{pds}/xrpc/com.atproto.server.refreshSession",
                headers={"Authorization": f"Bearer {refresh_jwt}"},
            )
    except httpx.RequestError as exc:
        raise BlueskyError(f"Could not reach PDS {pds}") from exc

    if response.status_code in (400, 401):
        raise BlueskyAuthError("Session expired; re-authenticate with app password")
    if response.status_code >= 400:
        raise BlueskyError(f"PDS returned HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise BlueskyError("PDS returned an invalid refresh response") from exc

    access_jwt = data.get("accessJwt")
    new_refresh = data.get("refreshJwt") or refresh_jwt
    did = data.get("did")
    if not isinstance(access_jwt, str) or not access_jwt or not isinstance(did, str):
        raise BlueskyError("PDS returned an incomplete refresh response")
    return {"did": did, "access_jwt": access_jwt, "refresh_jwt": new_refresh}


def session_expiry(access_jwt: str) -> str:
    """SQLite timestamp for when a cached access JWT should be refreshed."""
    exp = _decode_jwt_exp(access_jwt)
    if exp is None:
        exp = datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_SESSION_TTL_SECONDS)
    else:
        # Refresh a minute early to avoid racing the actual expiry.
        exp = exp - timedelta(seconds=60)
    return exp.strftime("%Y-%m-%d %H:%M:%S")


# ── Rich text facets ─────────────────────────────────────────────────────────


_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_FACET_LINK_TYPE = "app.bsky.richtext.facet#link"


def build_facets(text: str) -> list[dict]:
    """Find URLs in post text and build link facets with UTF-8 byte offsets.

    Byte offsets are relative to the UTF-8 encoding of the text, as required
    by app.bsky.richtext.facet. Trailing punctuation is trimmed from each URL
    so it is not swallowed into the link.
    """
    encoded = text.encode("utf-8")
    facets: list[dict] = []

    for match in _URL_RE.finditer(text):
        uri = match.group(0)
        # Strip trailing punctuation that is unlikely to be part of the URL.
        while uri and uri[-1] in ".,;:!?'\"()[]{}<>":
            uri = uri[:-1]
        if not uri:
            continue

        # Locate the trimmed URI's byte range inside the encoded text.
        char_start = match.start()
        byte_start = len(text[:char_start].encode("utf-8"))
        byte_end = byte_start + len(uri.encode("utf-8"))

        facets.append(
            {
                "index": {"byteStart": byte_start, "byteEnd": byte_end},
                "features": [
                    {"$type": _FACET_LINK_TYPE, "uri": uri},
                ],
            }
        )

    return facets


# ── Grapheme-aware truncation ────────────────────────────────────────────────


_ZWJ = "\u200d"
_VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}
_SKIN_TONE_RANGE = range(0x1F3FB, 0x1F400)


def _grapheme_clusters(text: str) -> list[str]:
    """Split text into grapheme clusters using a conservative UAX #29 subset.

    A new grapheme starts at any character that is not a combining mark,
    zero-width joiner, variation selector, or skin-tone modifier. ZWJ glues in
    both directions (GB9 + a permissive GB11 stand-in), so ZWJ emoji sequences
    like family emoji stay together. Over-merging is deliberate: it can only
    under-count graphemes, so truncation never exceeds platform limits.
    """
    clusters: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        is_continuation = (
            cat in ("Mn", "Mc", "Me")
            or ch in _VARIATION_SELECTORS
            or ord(ch) in _SKIN_TONE_RANGE
        )
        if clusters:
            is_continuation = is_continuation or ch == _ZWJ or clusters[-1][-1] == _ZWJ
        if clusters and is_continuation:
            clusters[-1] += ch
        else:
            clusters.append(ch)
    return clusters


def truncate_graphemes(text: str, max_graphemes: int = MAX_POST_GRAPHEMES) -> str:
    """Truncate to max_graphemes grapheme clusters, appending an ellipsis."""
    clusters = _grapheme_clusters(text)
    if len(clusters) <= max_graphemes:
        return text
    return "".join(clusters[: max_graphemes - 1]).rstrip() + "…"


# ── Posts and images ─────────────────────────────────────────────────────────


def upload_blob(
    pds: str, access_jwt: str, image_bytes: bytes, content_type: str
) -> dict | None:
    """Upload an image blob. Returns the blob reference dict, or None on failure."""
    pds = pds.rstrip("/")
    validate_outbound_url(pds)

    try:
        with httpx.Client(
            timeout=httpx.Timeout(60.0), follow_redirects=False
        ) as client:
            response = client.post(
                f"{pds}/xrpc/com.atproto.repo.uploadBlob",
                headers={
                    "Authorization": f"Bearer {access_jwt}",
                    "Content-Type": content_type,
                },
                content=image_bytes,
            )
    except httpx.RequestError:
        return None

    if response.status_code >= 400:
        return None

    try:
        blob = response.json().get("blob")
    except ValueError:
        return None
    if not isinstance(blob, dict) or "ref" not in blob:
        return None
    return blob


def create_post(
    pds: str,
    access_jwt: str,
    repo: str,
    text: str,
    facets: list[dict] | None = None,
    embed: dict | None = None,
) -> dict:
    """Create a post record. Returns {uri, cid}. Raises BlueskyAuthError on 401."""
    pds = pds.rstrip("/")
    validate_outbound_url(pds)

    record: dict = {
        "text": text,
        "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if facets:
        record["facets"] = facets
    if embed:
        record["embed"] = embed

    payload = {"repo": repo, "collection": POST_COLLECTION, "record": record}

    try:
        with httpx.Client(
            timeout=httpx.Timeout(30.0), follow_redirects=False
        ) as client:
            response = client.post(
                f"{pds}/xrpc/com.atproto.repo.createRecord",
                headers={"Authorization": f"Bearer {access_jwt}"},
                json=payload,
            )
    except httpx.RequestError as exc:
        raise BlueskyError(f"Could not reach PDS {pds}") from exc

    if response.status_code in (400, 401):
        # 400 covers ExpiredToken and other auth-class errors for this route.
        raise BlueskyAuthError(f"Post rejected (HTTP {response.status_code})")
    if response.status_code >= 400:
        raise BlueskyError(f"PDS returned HTTP {response.status_code}")

    try:
        data = response.json()
    except ValueError as exc:
        raise BlueskyError("PDS returned an invalid post response") from exc
    if not isinstance(data.get("uri"), str) or not isinstance(data.get("cid"), str):
        raise BlueskyError("PDS returned an incomplete post response")
    return {"uri": data["uri"], "cid": data["cid"]}


def build_image_embed(blob: dict, alt_text: str) -> dict:
    """Wrap an uploaded blob in an app.bsky.embed.images embed."""
    alt = truncate_graphemes((alt_text or "").strip(), MAX_ALT_GRAPHEMES)
    return {
        "$type": "app.bsky.embed.images",
        "images": [{"alt": alt, "image": blob}],
    }


# ── Connection testing ───────────────────────────────────────────────────────


def test_connection(handle: str, app_password: str) -> tuple[bool, str]:
    """Resolve the handle and verify the app password works."""
    try:
        handle = normalize_handle(handle)
        did, pds = resolve_pds(handle)
        session = create_session(pds, handle, app_password)
        return True, f"Connected as @{handle} ({session['did'][:20]}…)"
    except ValueError as e:
        return False, str(e)
    except BlueskyAuthError:
        return False, "Invalid handle or app password"
    except BlueskyError as e:
        return False, str(e)
