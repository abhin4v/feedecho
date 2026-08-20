"""Tests for Bluesky integration: module helpers, dispatch, and API routes."""

import os
import tempfile

import pytest


@pytest.fixture()
def db_tmp(monkeypatch):
    """Point the DB layer at a fresh temp file per test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)

    import database

    monkeypatch.setattr(database, "DB_PATH", database.Path(path))
    database.init_db()

    import scheduler

    monkeypatch.setattr(scheduler, "get_db", database.get_db)

    yield database

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _item(**overrides):
    item = {
        "id": "item-1",
        "title": "Test Post",
        "link": "https://example.com/post/1",
        "summary": "A summary of the post.",
        "image_url": "",
    }
    item.update(overrides)
    return item


def _setup_bluesky_echo(db_tmp, echo_overrides=None):
    """Create a Bluesky account, feed, and echo. Returns the echo row."""
    import database

    echo_kwargs = {
        "destination_type": "bluesky",
        "destination_id": 1,
        "template": "{{ title }} {{ link }}",
        "visibility": "public",
        "filter_keywords": "",
        "filter_mode": "exclude",
        "content_warning": "",
        "attach_image": 0,
        "enabled": 1,
    }
    if echo_overrides:
        echo_kwargs.update(echo_overrides)

    with database.get_db() as db:
        db.execute(
            """INSERT INTO bluesky_accounts (name, handle, app_password, did, pds)
               VALUES (?, ?, ?, ?, ?)""",
            ("main", "user.bsky.social", "abcd-efgh-ijkl-mnop", "did:plc:test123", "https://bsky.social"),
        )
        db.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("f", "https://example.com/feed"),
        )
        db.execute(
            """INSERT INTO echoes (feed_id, destination_type, destination_id, template,
                                   visibility, filter_keywords, filter_mode,
                                   content_warning, attach_image, enabled)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                echo_kwargs["destination_type"],
                echo_kwargs["destination_id"],
                echo_kwargs["template"],
                echo_kwargs["visibility"],
                echo_kwargs["filter_keywords"],
                echo_kwargs["filter_mode"],
                echo_kwargs["content_warning"],
                echo_kwargs["attach_image"],
                echo_kwargs["enabled"],
            ),
        )
        return db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()


# ── Handle normalization ─────────────────────────────────────────────────────


class TestNormalizeHandle:
    def test_lowercases_and_strips_at(self):
        from bluesky import normalize_handle

        assert normalize_handle("@User.bsky.Social") == "user.bsky.social"

    def test_strips_profile_url(self):
        from bluesky import normalize_handle

        assert normalize_handle("https://bsky.app/profile/name.bsky.social") == "name.bsky.social"

    def test_strips_whitespace(self):
        from bluesky import normalize_handle

        assert normalize_handle("  name.example.com  ") == "name.example.com"

    def test_rejects_garbage(self):
        from bluesky import normalize_handle

        with pytest.raises(ValueError):
            normalize_handle("")
        with pytest.raises(ValueError):
            normalize_handle("no-dot-here")
        with pytest.raises(ValueError):
            normalize_handle("has spaces.example.com")


# ── Grapheme-aware truncation ────────────────────────────────────────────────


class TestTruncateGraphemes:
    def test_short_text_unchanged(self):
        from bluesky import truncate_graphemes

        assert truncate_graphemes("short", 300) == "short"

    def test_long_ascii_truncated_with_ellipsis(self):
        from bluesky import truncate_graphemes

        result = truncate_graphemes("a" * 500, 300)
        assert len(result) == 300
        assert result == "a" * 299 + "…"

    def test_emoji_counted_as_single_grapheme(self):
        from bluesky import truncate_graphemes

        result = truncate_graphemes("👍" * 400, 300)
        assert len(result) == 300
        assert result == "👍" * 299 + "…"

    def test_combining_marks_stay_with_base(self):
        from bluesky import truncate_graphemes
        from bluesky import _grapheme_clusters

        # 'e' + combining acute accent is one grapheme
        assert _grapheme_clusters("e\u0301") == ["e\u0301"]
        # 2 graphemes fit in max_graphemes=2 without truncation
        assert truncate_graphemes("e\u0301x", 2) == "e\u0301x"

    def test_zwj_emoji_family_single_grapheme(self):
        from bluesky import _grapheme_clusters

        family = "\U0001F468\u200D\U0001F469\u200D\U0001F467"
        clusters = _grapheme_clusters(family + "!")
        assert clusters == [family, "!"]


# ── Facets ───────────────────────────────────────────────────────────────────


class TestBuildFacets:
    def test_no_urls_returns_empty(self):
        from bluesky import build_facets

        assert build_facets("plain text") == []

    def test_single_url_byte_offsets(self):
        from bluesky import build_facets

        text = "read https://example.com/a now"
        facets = build_facets(text)
        assert len(facets) == 1
        facet = facets[0]
        start, end = facet["index"]["byteStart"], facet["index"]["byteEnd"]
        assert text.encode("utf-8")[start:end].decode() == "https://example.com/a"
        assert facet["features"][0]["$type"] == "app.bsky.richtext.facet#link"
        assert facet["features"][0]["uri"] == "https://example.com/a"

    def test_multibyte_prefix_offsets(self):
        from bluesky import build_facets

        prefix = "héllo 👋 "
        uri = "https://example.com/1"
        facets = build_facets(prefix + uri)
        start, end = facets[0]["index"]["byteStart"], facets[0]["index"]["byteEnd"]
        assert start == len(prefix.encode("utf-8"))
        assert end == len((prefix + uri).encode("utf-8"))

    def test_trailing_punctuation_trimmed(self):
        from bluesky import build_facets

        facets = build_facets("see https://example.com/x.")
        assert facets[0]["features"][0]["uri"] == "https://example.com/x"

    def test_multiple_urls(self):
        from bluesky import build_facets

        facets = build_facets("a https://a.example.com b https://b.example.com")
        assert len(facets) == 2
        assert [f["features"][0]["uri"] for f in facets] == [
            "https://a.example.com",
            "https://b.example.com",
        ]


# ── Session expiry ───────────────────────────────────────────────────────────


class TestSessionExpiry:
    def test_decodes_jwt_exp(self):
        import base64
        import json
        from datetime import datetime, timedelta, timezone

        from bluesky import session_expiry

        exp = datetime.now(timezone.utc) + timedelta(hours=1)
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": int(exp.timestamp())}).encode()
        ).rstrip(b"=")
        jwt = f"h.{payload.decode()}.s"
        parsed = datetime.strptime(
            session_expiry(jwt), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        delta = (exp - timedelta(seconds=60) - parsed).total_seconds()
        assert abs(delta) < 2

    def test_undecodable_jwt_defaults_to_two_hours(self):
        from datetime import datetime, timedelta, timezone

        from bluesky import session_expiry

        parsed = datetime.strptime(session_expiry("not-a-jwt"), "%Y-%m-%d %H:%M:%S")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        assert timedelta(hours=1, minutes=50) < (parsed - now) < timedelta(hours=2)


# ── Scheduler dispatch ───────────────────────────────────────────────────────


def _stub_session(monkeypatch):
    """Stub Bluesky session functions so no network I/O happens in tests."""
    import scheduler

    monkeypatch.setattr(
        scheduler,
        "resolve_pds",
        lambda handle: ("did:plc:test123", "https://bsky.social"),
    )
    monkeypatch.setattr(
        scheduler,
        "create_session",
        lambda pds, handle, pw: {
            "did": "did:plc:test123",
            "access_jwt": "aj",
            "refresh_jwt": "rj",
        },
    )
    monkeypatch.setattr(
        scheduler,
        "refresh_session",
        lambda pds, rj: {
            "did": "did:plc:test123",
            "access_jwt": "refreshed-aj",
            "refresh_jwt": "refreshed-rj",
        },
    )


class TestSendBluesky:
    def test_happy_path_posts_and_records_success(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        assert len(sent) == 1
        assert sent[0]["repo"] == "did:plc:test123"
        assert sent[0]["text"] == "Test Post https://example.com/post/1"
        assert sent[0]["facets"]
        assert sent[0]["embed"] is None

        import database

        with database.get_db() as db:
            row = db.execute("SELECT status FROM posted_items WHERE echo_id = 1").fetchone()
            assert row["status"] == "success"

    def test_content_truncated_to_300_graphemes(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)

        echo = _setup_bluesky_echo(db_tmp)
        item = _item(title="x" * 500, link="")
        scheduler.process_echo(echo, item)

        assert len(sent[0]["text"]) == 300
        assert sent[0]["text"].endswith("…")

    def test_missing_account_fails_post(self, db_tmp, monkeypatch):
        import database
        import scheduler

        echo = _setup_bluesky_echo(db_tmp)
        with database.get_db() as db:
            db.execute("DELETE FROM bluesky_accounts")

        ok = scheduler.process_echo(echo, _item())

        assert ok is False
        with database.get_db() as db:
            row = db.execute(
                "SELECT status, error_message FROM posted_items WHERE echo_id = 1"
            ).fetchone()
            assert row["status"] == "failed"
            assert "not found" in row["error_message"]

    def test_image_attached_when_enabled(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"fake-image-bytes", "image/jpeg")
        )
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: {"$type": "blob", "ref": {"$link": "bafkreifake"}},
        )
        import alt_text

        monkeypatch.setattr(alt_text, "is_enabled", lambda: False)

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/photo.jpg")
        scheduler.process_echo(echo, item)

        assert sent[0]["embed"]["$type"] == "app.bsky.embed.images"
        assert sent[0]["embed"]["images"][0]["image"]["ref"]["$link"] == "bafkreifake"
        assert sent[0]["embed"]["images"][0]["alt"] == ""

    def test_unsupported_image_type_posts_text_only(self, db_tmp, monkeypatch):
        import scheduler

        sent = []
        upload_calls = []
        monkeypatch.setattr(
            scheduler, "create_post", lambda **kw: sent.append(kw) or {"uri": "u", "cid": "c"}
        )
        _stub_session(monkeypatch)
        monkeypatch.setattr(
            scheduler, "fetch_image", lambda url: (b"bytes", "image/avif")
        )
        monkeypatch.setattr(
            scheduler,
            "upload_blob",
            lambda **kw: upload_calls.append(kw) or {"$type": "blob"},
        )

        echo = _setup_bluesky_echo(db_tmp, {"attach_image": 1})
        item = _item(image_url="https://example.com/photo.avif")
        scheduler.process_echo(echo, item)

        assert len(upload_calls) == 0
        assert sent[0]["embed"] is None

    def test_auth_error_retries_with_fresh_session(self, db_tmp, monkeypatch):
        import database
        import scheduler

        calls = {"count": 0}
        from bluesky import BlueskyAuthError

        def flaky_post(**kw):
            calls["count"] += 1
            if calls["count"] == 1:
                raise BlueskyAuthError("ExpiredToken")
            return {"uri": "u", "cid": "c"}

        monkeypatch.setattr(scheduler, "create_post", flaky_post)
        _stub_session(monkeypatch)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is True
        assert calls["count"] == 2
        with database.get_db() as db:
            row = db.execute(
                "SELECT access_jwt FROM bluesky_accounts WHERE id = 1"
            ).fetchone()
            assert row["access_jwt"] == "aj"  # refreshed via stub create_session

    def test_persistent_failure_marks_failed(self, db_tmp, monkeypatch):
        import database
        import scheduler

        from bluesky import BlueskyAuthError

        def always_fail(**kw):
            raise BlueskyAuthError("InvalidToken")

        monkeypatch.setattr(scheduler, "create_post", always_fail)
        _stub_session(monkeypatch)

        echo = _setup_bluesky_echo(db_tmp)
        ok = scheduler.process_echo(echo, _item())

        assert ok is False
        with database.get_db() as db:
            row = db.execute("SELECT status FROM posted_items WHERE echo_id = 1").fetchone()
            assert row["status"] == "failed"


# ── Session caching ──────────────────────────────────────────────────────────


def _insert_bsky_account(db, **overrides):
    """Insert a Bluesky account row and return it."""
    values = {
        "name": "main",
        "handle": "user.bsky.social",
        "app_password": "abcd-efgh-ijkl-mnop",
        "did": "did:plc:test123",
        "pds": "https://bsky.social",
        "access_jwt": "",
        "refresh_jwt": "",
        "session_expires_at": None,
    }
    values.update(overrides)
    db.execute(
        """INSERT INTO bluesky_accounts
             (name, handle, app_password, did, pds, access_jwt, refresh_jwt, session_expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            values["name"],
            values["handle"],
            values["app_password"],
            values["did"],
            values["pds"],
            values["access_jwt"],
            values["refresh_jwt"],
            values["session_expires_at"],
        ),
    )
    return db.execute(
        "SELECT * FROM bluesky_accounts WHERE handle = ?", (values["handle"],)
    ).fetchone()


class TestBskySession:
    def test_reuses_cached_valid_session(self, db_tmp, monkeypatch):
        import database
        import scheduler

        monkeypatch.setattr(
            scheduler, "refresh_session", lambda *a, **kw: pytest.fail("should not refresh")
        )
        monkeypatch.setattr(
            scheduler, "create_session", lambda *a, **kw: pytest.fail("should not create")
        )

        with database.get_db() as db:
            account = _insert_bsky_account(
                db, access_jwt="cached-aj", session_expires_at="2099-01-01 00:00:00"
            )
            session = scheduler._bsky_session(db, account)

        assert session["access_jwt"] == "cached-aj"
        assert session["did"] == "did:plc:test123"

    def test_refreshes_expired_session(self, db_tmp, monkeypatch):
        import database
        import scheduler

        monkeypatch.setattr(
            scheduler,
            "refresh_session",
            lambda pds, rj: {"did": "did:plc:test123", "access_jwt": "new-aj", "refresh_jwt": "new-rj"},
        )
        monkeypatch.setattr(
            scheduler, "create_session", lambda *a, **kw: pytest.fail("should not create")
        )

        with database.get_db() as db:
            account = _insert_bsky_account(
                db, refresh_jwt="old-rj", session_expires_at="2000-01-01 00:00:00"
            )
            session = scheduler._bsky_session(db, account)

        assert session["access_jwt"] == "new-aj"
        with database.get_db() as db:
            row = db.execute(
                "SELECT access_jwt, refresh_jwt FROM bluesky_accounts WHERE id = 1"
            ).fetchone()
            assert row["access_jwt"] == "new-aj"
            assert row["refresh_jwt"] == "new-rj"

    def test_falls_back_to_login_when_refresh_fails(self, db_tmp, monkeypatch):
        import database
        import scheduler

        from bluesky import BlueskyAuthError

        def fail_refresh(pds, rj):
            raise BlueskyAuthError("ExpiredToken")

        monkeypatch.setattr(scheduler, "refresh_session", fail_refresh)
        monkeypatch.setattr(
            scheduler,
            "create_session",
            lambda pds, handle, pw: {
                "did": "did:plc:test123",
                "access_jwt": "login-aj",
                "refresh_jwt": "login-rj",
            },
        )

        with database.get_db() as db:
            account = _insert_bsky_account(
                db, refresh_jwt="old-rj", session_expires_at="2000-01-01 00:00:00"
            )
            session = scheduler._bsky_session(db, account)

        assert session["access_jwt"] == "login-aj"


# ── API routes ───────────────────────────────────────────────────────────────


class TestBlueskyAccountRoutes:
    @pytest.fixture()
    def client(self, db_tmp, monkeypatch):
        import app as app_module

        monkeypatch.setattr(app_module, "_AUTH_TOKEN", None)
        monkeypatch.setattr(app_module, "bluesky_session_expiry", lambda jwt: "2099-01-01 00:00:00")

        from fastapi.testclient import TestClient

        return TestClient(app_module.app)

    def test_add_account_verifies_and_stores(self, client, monkeypatch):
        import app as app_module
        import database

        monkeypatch.setattr(
            app_module, "bluesky_resolve_pds", lambda handle: ("did:plc:abc", "https://bsky.social")
        )
        monkeypatch.setattr(
            app_module,
            "bluesky_create_session",
            lambda pds, handle, pw: {
                "did": "did:plc:abc",
                "access_jwt": "aj",
                "refresh_jwt": "rj",
            },
        )

        resp = client.post(
            "/api/bluesky-accounts",
            data={"name": "My Bsky", "handle": "@User.Bsky.Social", "app_password": "abcd-efgh-ijkl-mnop"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "bluesky_connected" in resp.headers["location"]

        with database.get_db() as db:
            row = db.execute(
                "SELECT * FROM bluesky_accounts WHERE handle = 'user.bsky.social'"
            ).fetchone()
            assert row is not None
            assert row["name"] == "My Bsky"
            assert row["did"] == "did:plc:abc"
            assert row["pds"] == "https://bsky.social"
            assert row["access_jwt"] == "aj"

    def test_add_account_bad_password_shows_error(self, client, monkeypatch):
        import app as app_module
        import database

        from bluesky import BlueskyAuthError

        monkeypatch.setattr(
            app_module, "bluesky_resolve_pds", lambda handle: ("did:plc:abc", "https://bsky.social")
        )
        monkeypatch.setattr(
            app_module,
            "bluesky_create_session",
            lambda pds, handle, pw: (_ for _ in ()).throw(BlueskyAuthError("nope")),
        )

        resp = client.post(
            "/api/bluesky-accounts",
            data={"name": "Bad", "handle": "user.bsky.social", "app_password": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "app password" in resp.text

        with database.get_db() as db:
            count = db.execute("SELECT COUNT(*) as c FROM bluesky_accounts").fetchone()["c"]
            assert count == 0

    def test_add_account_invalid_handle_shows_error(self, client):
        resp = client.post(
            "/api/bluesky-accounts",
            data={"name": "Bad", "handle": "not a handle", "app_password": "x"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "handle" in resp.text

    def test_test_endpoint_reports_success(self, client, monkeypatch):
        import app as app_module
        import database

        with database.get_db() as db:
            db.execute(
                """INSERT INTO bluesky_accounts (name, handle, app_password)
                   VALUES ('main', 'user.bsky.social', 'pw')"""
            )

        monkeypatch.setattr(
            app_module, "test_bluesky_connection", lambda h, p: (True, "Connected as @user.bsky.social")
        )
        resp = client.post("/api/bluesky-accounts/1/test")
        data = resp.json()
        assert data["success"] is True
        assert "user.bsky.social" in data["message"]

    def test_delete_endpoint_removes_account(self, client):
        import database

        with database.get_db() as db:
            db.execute(
                """INSERT INTO bluesky_accounts (name, handle, app_password)
                   VALUES ('main', 'user.bsky.social', 'pw')"""
            )

        resp = client.post("/api/bluesky-accounts/1/delete", follow_redirects=False)
        assert resp.status_code == 303

        with database.get_db() as db:
            count = db.execute("SELECT COUNT(*) as c FROM bluesky_accounts").fetchone()["c"]
            assert count == 0


# ── Echo API validation ──────────────────────────────────────────────────────


class TestEchoDestinationValidation:
    @pytest.fixture()
    def client(self, db_tmp, monkeypatch):
        import app as app_module

        monkeypatch.setattr(app_module, "_AUTH_TOKEN", None)
        from fastapi.testclient import TestClient

        return TestClient(app_module.app)

    def _add_bluesky_account(self):
        import database

        with database.get_db() as db:
            db.execute(
                """INSERT INTO bluesky_accounts (name, handle, app_password)
                   VALUES ('main', 'user.bsky.social', 'pw')"""
            )
            db.execute(
                "INSERT INTO feeds (name, url) VALUES ('f', 'https://example.com/feed')"
            )

    def test_create_echo_for_bluesky(self, client):
        import database

        self._add_bluesky_account()
        resp = client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "bluesky",
                "bluesky_account_id": "1",
                "template": "{{ title }} {{ link }}",
                "visibility": "public",
                "filter_mode": "exclude",
                "delivery_mode": "instant",
                "enabled": "true",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with database.get_db() as db:
            row = db.execute("SELECT * FROM echoes WHERE id = 1").fetchone()
            assert row["destination_type"] == "bluesky"
            assert row["destination_id"] == 1

    def test_create_echo_bluesky_requires_account(self, client):
        self._add_bluesky_account()
        resp = client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "bluesky",
                "template": "{{ title }}",
                "visibility": "public",
                "filter_mode": "exclude",
                "delivery_mode": "instant",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_invalid_destination_type_rejected(self, client):
        self._add_bluesky_account()
        resp = client.post(
            "/api/echoes",
            data={
                "feed_id": "1",
                "destination_type": "carrier-pigeon",
                "account_id": "1",
                "visibility": "public",
                "filter_mode": "exclude",
                "delivery_mode": "instant",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
