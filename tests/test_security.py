"""Tests for security features: SSRF protection and OAuth state signing."""

import pytest
import socket
from unittest.mock import patch, MagicMock
from feed_parser import validate_outbound_url, validate_feed_url, SSRFError
from oauth import _sign_state, _verify_state, get_or_create_app
from mastodon import post_status, verify_credentials


class TestSSRFProtection:
    """SSRF filter blocks private/internal IPs and non-http schemes."""

    def test_blocks_localhost_ip(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://127.0.0.1/latest/meta-data/")

    def test_blocks_loopback_ipv6(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://[::1]/test")

    def test_blocks_private_10(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://10.0.0.1/internal")

    def test_blocks_private_192(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://192.168.1.1/admin")

    def test_blocks_private_172(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://172.16.0.1/")

    def test_blocks_link_local(self):
        with pytest.raises(SSRFError, match="private"):
            validate_outbound_url("http://169.254.169.254/latest/meta-data/")

    def test_blocks_file_scheme(self):
        with pytest.raises(SSRFError, match="not allowed"):
            validate_outbound_url("file:///etc/passwd")

    def test_blocks_gopher_scheme(self):
        with pytest.raises(SSRFError, match="not allowed"):
            validate_outbound_url("gopher://localhost/")

    def test_blocks_embedded_credentials(self):
        with pytest.raises(SSRFError, match="credentials"):
            validate_outbound_url("http://user:pass@example.com/feed")

    def test_blocks_hostname_resolving_to_private(self):
        """Hostnames that resolve to private IPs should be blocked."""
        with patch("socket.getaddrinfo") as mock_resolve:
            mock_resolve.return_value = [
                (socket.AF_INET, 0, 0, "", ("10.0.0.5", 0))
            ]
            with pytest.raises(SSRFError, match="resolves to"):
                validate_outbound_url("http://internal.example.com/secret")

    def test_allows_public_ip(self):
        # 8.8.8.8 is Google DNS — public, not blocked
        result = validate_outbound_url("https://8.8.8.8/feed.xml")
        assert result == "https://8.8.8.8/feed.xml"

    def test_allows_normal_https_url(self):
        result = validate_outbound_url("https://example.com/feed.xml")
        assert result == "https://example.com/feed.xml"

    def test_validate_feed_url_alias_works(self):
        """The backwards-compatible alias should work identically."""
        result = validate_feed_url("https://example.com/feed.xml")
        assert result == "https://example.com/feed.xml"


class TestSSRFRedirectProtection:
    """SSRF protection validates every redirect hop, not just the initial URL."""

    def test_redirect_to_private_ip_is_blocked(self):
        """A feed at a public URL that redirects to a private IP must be blocked.

        Simulates: http://evil.example/feed -> 302 -> http://169.254.169.254/
        """
        from feed_parser import _fetch_with_redirect_validation, MAX_REDIRECTS

        # Mock httpx client that returns a redirect to a private IP
        mock_response_redirect = MagicMock()
        mock_response_redirect.is_redirect = True
        mock_response_redirect.headers = {"location": "http://169.254.169.254/latest/meta-data/"}

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response_redirect

        with pytest.raises(SSRFError, match="private"):
            _fetch_with_redirect_validation(
                mock_client, "https://evil.example/feed.xml", {}
            )

    def test_redirect_to_localhost_is_blocked(self):
        """A redirect to localhost must be blocked."""
        from feed_parser import _fetch_with_redirect_validation

        mock_response_redirect = MagicMock()
        mock_response_redirect.is_redirect = True
        mock_response_redirect.headers = {"location": "http://127.0.0.1:8080/admin"}

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response_redirect

        with pytest.raises(SSRFError, match="private"):
            _fetch_with_redirect_validation(
                mock_client, "https://evil.example/feed.xml", {}
            )

    def test_redirect_to_public_url_allowed(self):
        """A redirect to another public URL should be followed."""
        from feed_parser import _fetch_with_redirect_validation

        # First response: redirect to a public URL
        mock_response_redirect = MagicMock()
        mock_response_redirect.is_redirect = True
        mock_response_redirect.headers = {"location": "https://8.8.8.8/feed.xml"}

        # Second response: actual content (not a redirect)
        mock_response_final = MagicMock()
        mock_response_final.is_redirect = False
        mock_response_final.status_code = 200

        mock_client = MagicMock()
        mock_client.get.side_effect = [mock_response_redirect, mock_response_final]

        result = _fetch_with_redirect_validation(
            mock_client, "https://8.8.8.8/feed.xml", {}
        )
        assert result == mock_response_final

    def test_too_many_redirects_raises(self):
        """Exceeding MAX_REDIRECTS should raise ValueError."""
        from feed_parser import _fetch_with_redirect_validation, MAX_REDIRECTS

        mock_response_redirect = MagicMock()
        mock_response_redirect.is_redirect = True
        mock_response_redirect.headers = {"location": "https://8.8.8.8/feed.xml"}

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response_redirect

        with pytest.raises(ValueError, match="Too many redirects"):
            _fetch_with_redirect_validation(
                mock_client, "https://8.8.8.8/feed.xml", {}
            )

    def test_relative_redirect_is_resolved_and_validated(self):
        """Relative redirects should be resolved against the current URL."""
        from feed_parser import _fetch_with_redirect_validation

        # First response: relative redirect
        mock_response_redirect = MagicMock()
        mock_response_redirect.is_redirect = True
        mock_response_redirect.headers = {"location": "/feed.xml"}

        # Second response: actual content
        mock_response_final = MagicMock()
        mock_response_final.is_redirect = False
        mock_response_final.status_code = 200

        mock_client = MagicMock()
        mock_client.get.side_effect = [mock_response_redirect, mock_response_final]

        result = _fetch_with_redirect_validation(
            mock_client, "https://8.8.8.8/old-feed", {}
        )
        assert result == mock_response_final


class TestInstanceURLSSRF:
    """Mastodon/OAuth instance URLs are validated for SSRF."""

    def test_oauth_get_or_create_app_validates_instance(self):
        """get_or_create_app should reject private IPs for instance URLs."""
        with pytest.raises(SSRFError):
            get_or_create_app("http://127.0.0.1:8000")

    def test_mastodon_post_status_validates_instance(self):
        """post_status should reject private IPs for instance URLs."""
        with pytest.raises(SSRFError):
            post_status(
                instance="http://10.0.0.1",
                access_token="fake-token",
                content="test",
            )

    def test_mastodon_verify_credentials_validates_instance(self):
        """verify_credentials should reject private IPs for instance URLs."""
        with pytest.raises(SSRFError):
            verify_credentials(
                instance="http://169.254.169.254",
                access_token="fake-token",
            )

    def test_oauth_get_or_create_app_allows_public_instance(self):
        """Public instance URLs should pass validation (but may fail on network)."""
        with patch("oauth.validate_outbound_url") as mock_validate:
            mock_validate.return_value = "https://dmv.community"
            # This will fail at the DB/cache lookup stage, not at validation
            try:
                get_or_create_app("https://dmv.community")
            except Exception:
                pass  # Expected — we just want to confirm validate was called
            mock_validate.assert_called_once_with("https://dmv.community")


class TestOAuthStateSigning:
    """HMAC-signed state tokens prevent CSRF and tampering."""

    def test_sign_and_verify_roundtrip(self):
        instance = "https://dmv.community"
        token = _sign_state(instance)
        assert _verify_state(token) == instance

    def test_verify_rejects_tampered_instance(self):
        token = _sign_state("https://dmv.community")
        # Tamper: replace instance with a different one
        parts = token.rsplit("|", 2)
        parts[1] = "https://evil.example"
        tampered = "|".join(parts)
        with pytest.raises(ValueError, match="Invalid state signature"):
            _verify_state(tampered)

    def test_verify_rejects_tampered_signature(self):
        token = _sign_state("https://dmv.community")
        parts = token.rsplit("|", 2)
        parts[2] = "a" * 16  # wrong signature
        tampered = "|".join(parts)
        with pytest.raises(ValueError, match="Invalid state signature"):
            _verify_state(tampered)

    def test_verify_rejects_malformed_state(self):
        with pytest.raises(ValueError, match="Invalid state"):
            _verify_state("just-a-string")

    def test_verify_rejects_empty_state(self):
        with pytest.raises(ValueError, match="Invalid state"):
            _verify_state("")

    def test_state_contains_random_nonce(self):
        """Each call should produce a different token (unique nonce)."""
        token1 = _sign_state("https://dmv.community")
        token2 = _sign_state("https://dmv.community")
        assert token1 != token2  # different nonces
