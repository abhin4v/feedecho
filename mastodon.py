"""Mastodon API client — post statuses via the Mastodon REST API."""

import httpx
from typing import Optional


def post_status(
    instance: str,
    access_token: str,
    content: str,
    visibility: str = "public",
    sensitive: bool = False,
) -> dict:
    """Post a status to a Mastodon instance.

    Args:
        instance: Base URL of the instance (e.g. "https://dmv.community")
        access_token: OAuth access token
        content: The status text
        visibility: public, unlisted, private, or direct
        sensitive: Mark as sensitive content

    Returns:
        Dict with response data including 'id' and 'url' on success.

    Raises:
        httpx.HTTPStatusError on API failure.
    """
    instance = instance.rstrip("/")
    url = f"{instance}/api/v1/statuses"
    headers = {"Authorization": f"Bearer {access_token}"}
    data = {
        "status": content,
        "visibility": visibility,
        "sensitive": sensitive,
    }

    with httpx.Client(timeout=30) as client:
        response = client.post(url, headers=headers, data=data)
        response.raise_for_status()
        return response.json()


def verify_credentials(instance: str, access_token: str) -> dict:
    """Verify credentials and return account info.

    Returns dict with 'username', 'display_name', 'url' on success.
    """
    instance = instance.rstrip("/")
    url = f"{instance}/api/v1/accounts/verify_credentials"
    headers = {"Authorization": f"Bearer {access_token}"}

    with httpx.Client(timeout=30) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()


def test_connection(instance: str, access_token: str) -> tuple[bool, str]:
    """Test a Mastodon connection. Returns (success, message)."""
    try:
        result = verify_credentials(instance, access_token)
        return True, f"Connected as @{result.get('username', 'unknown')}"
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except httpx.RequestError as e:
        return False, f"Network error: {e}"
