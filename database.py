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
                instance TEXT NOT NULL,
                access_token TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
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
        db.execute("""
            CREATE TABLE IF NOT EXISTS echoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feed_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                template TEXT NOT NULL DEFAULT '{{ title }} {{ link }}',
                visibility TEXT DEFAULT 'public',
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE,
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
                UNIQUE(feed_id, account_id)
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
