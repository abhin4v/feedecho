"""Database layer — SQLite with WAL mode, matches vinyl-catalog pattern."""

import sqlite3
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("FEEDCHO_DB_PATH", BASE_DIR / "feedecho.db"))


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT DEFAULT '',
                instance TEXT NOT NULL,
                access_token TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: add username column if missing, populate from existing names
        cols = db.execute("PRAGMA table_info(accounts)").fetchall()
        col_names = [c["name"] for c in cols]
        if "username" not in col_names:
            db.execute("ALTER TABLE accounts ADD COLUMN username TEXT DEFAULT ''")
            rows = db.execute("SELECT id, name FROM accounts").fetchall()
            import re
            for row in rows:
                m = re.search(r'\(([^)]+)\)$', row["name"] or "")
                if m:
                    db.execute("UPDATE accounts SET username = ? WHERE id = ?", (m.group(1), row["id"]))
                else:
                    db.execute("UPDATE accounts SET username = ? WHERE id = ?", (row["name"] or "unknown", row["id"]))
        db.execute("""
            CREATE TABLE IF NOT EXISTS feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                feed_type TEXT DEFAULT 'rss',
                poll_interval INTEGER DEFAULT 15,
                last_fetched TIMESTAMP,
                last_item_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migrate old echoes schema if needed
        cols = db.execute("PRAGMA table_info(echoes)").fetchall()
        col_names = [c["name"] for c in cols]
        if col_names and "account_id" in col_names and "destination_type" not in col_names:
            db.execute("DROP TABLE echoes")
        db.execute("""
            CREATE TABLE IF NOT EXISTS echoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER NOT NULL,
                destination_type TEXT NOT NULL DEFAULT 'mastodon',
                destination_id INTEGER NOT NULL,
                template TEXT NOT NULL DEFAULT '{{ title }} {{ link }}',
                visibility TEXT DEFAULT 'public',
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS email_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS posted_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                echo_id INTEGER NOT NULL,
                item_id TEXT NOT NULL,
                item_title TEXT,
                item_url TEXT,
                status TEXT NOT NULL,
                error_message TEXT,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (echo_id) REFERENCES echoes(id) ON DELETE CASCADE
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS oauth_apps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance TEXT NOT NULL UNIQUE,
                client_id TEXT NOT NULL,
                client_secret TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_posted_items_echo
            ON posted_items(echo_id, posted_at DESC)
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_echoes_feed
            ON echoes(feed_id)
        """)


init_db()
