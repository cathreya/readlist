"""SQLite schema and database helpers."""

import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lecture_today.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY,
            url TEXT NOT NULL UNIQUE,
            title TEXT,
            domain TEXT,
            content_type TEXT,
            description TEXT,
            estimated_minutes INTEGER,
            difficulty INTEGER CHECK(difficulty BETWEEN 1 AND 5),
            topic TEXT,
            blurb TEXT,
            chunk_total INTEGER DEFAULT 1,
            chunk_index INTEGER,
            parent_item_id INTEGER REFERENCES items(id),
            local_file_path TEXT,
            warmup TEXT,
            guided_questions TEXT,
            status TEXT DEFAULT 'unread',
            error_reason TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS item_prerequisites (
            item_id INTEGER REFERENCES items(id),
            prereq_id INTEGER REFERENCES items(id),
            confidence REAL CHECK(confidence BETWEEN 0 AND 1),
            PRIMARY KEY (item_id, prereq_id)
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            item_id INTEGER REFERENCES items(id),
            lecture_title TEXT,
            warmup TEXT,
            guided_questions TEXT,
            completed_at TEXT,
            exit_note TEXT,
            session_duration_minutes INTEGER
        );

        CREATE TABLE IF NOT EXISTS skips (
            id INTEGER PRIMARY KEY,
            item_id INTEGER REFERENCES items(id),
            reason TEXT CHECK(reason IN ('too_hard', 'not_right_now', 'not_interested', 'too_long')),
            skipped_at TEXT DEFAULT (datetime('now')),
            resurface_after TEXT
        );

        CREATE TABLE IF NOT EXISTS card_queue (
            id INTEGER PRIMARY KEY,
            item_id INTEGER REFERENCES items(id),
            session_id INTEGER REFERENCES sessions(id),
            front TEXT NOT NULL,
            back TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            flushed INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS classification_runs (
            id INTEGER PRIMARY KEY,
            run_at TEXT DEFAULT (datetime('now')),
            items_processed INTEGER,
            model_used TEXT
        );
    """)
    conn.commit()
    conn.close()

def compute_resurface_date(reason):
    now = datetime.utcnow()
    if reason == "too_hard":
        return (now + timedelta(days=14)).isoformat()
    elif reason == "not_right_now":
        return (now + timedelta(days=7)).isoformat()
    elif reason == "not_interested":
        return (now + timedelta(days=30)).isoformat()
    return (now + timedelta(days=7)).isoformat()
