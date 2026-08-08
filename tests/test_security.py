"""Tests for security features: SSRF protection and OAuth state signing."""

import pytest
import socket
from unittest.mock import patch
from feed_parser import validate_feed_url, SSRFError
from oauth import _sign_state, _verify_state


class TestSSRFProtection:
    """SSRF filter blocks private/internal IPs and non-http schemes."""

    def test_blocks_localhost_ip(self):
        with pytest.raises(SSRFError, match="private"):
            validate_feed_url("http://127.0.0.1/latest/meta-data/")

    def test_blocks_loopback_ipv6(self):
        with pytest.raises(SSRFError, match="private"):
            validate_feed_url("http://[::1]/test")

    def test_blocks_private_10(self):
        with pytest.raises(SSRFError, match="private"):
            validate_feed_url("http://10.0.0.1/internal")

    def test_blocks_private_192(self):
        with pytest.raises(SSRFError, match="private"):
            validate_feed_url("http://192.168.1.1/admin")

    def test_blocks_private_172(self):
        with pytest.raises(SSRFError, match="private"):
            validate_feed_url("http://172.16.0.1/")

    def test_blocks_link_local(self):
        with pytest.raises(SSRFError, match="private"):
            validate_feed_url("http://169.254.169.254/latest/meta-data/")

    def test_blocks_file_scheme(self):
        with pytest.raises(SSRFError, match="not allowed"):
            validate_feed_url("file:///etc/passwd")

    def test_blocks_gopher_scheme(self):
        with pytest.raises(SSRFError, match="not allowed"):
            validate_feed_url("gopher://localhost/")

    def test_blocks_hostname_resolving_to_private(self):
        """Hostnames that resolve to private IPs should be blocked."""
        with patch("socket.getaddrinfo") as mock_resolve:
            mock_resolve.return_value = [
                (socket.AF_INET, 0, 0, "", ("10.0.0.5", 0))
            ]
            with pytest.raises(SSRFError, match="resolves to"):
                validate_feed_url("http://internal.example.com/secret")

    def test_allows_public_ip(self):
        # 8.8.8.8 is Google DNS — public, not blocked
        result = validate_feed_url("https://8.8.8.8/feed.xml")
        assert result == "https://8.8.8.8/feed.xml"

    def test_allows_normal_https_url(self):
        result = validate_feed_url("https://example.com/feed.xml")
        assert result == "https://example.com/feed.xml"


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
