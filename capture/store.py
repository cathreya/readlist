"""SQLite persistence for Flashcap."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from flask import current_app, g
from fsrs import Card as FSRSCard
from fsrs import Rating, Scheduler, State

from .config import get_db_path, get_desired_retention, get_fsrs_fuzzing
from .taxonomy import normalize_category, normalize_tags

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    front         TEXT NOT NULL,
    back          TEXT NOT NULL,
    category      TEXT NOT NULL DEFAULT 'other',
    tags          TEXT NOT NULL DEFAULT '[]',
    source_url    TEXT,
    source_title  TEXT,
    source_site   TEXT,
    source_author TEXT,
    selection     TEXT,
    context       TEXT,
    quote         TEXT,
    hint          TEXT,
    confidence    REAL,
    model         TEXT,
    generated_by  TEXT NOT NULL DEFAULT 'llm',
    obsidian_path TEXT,
    obsidian_url  TEXT,
    due_at        TEXT,
    interval_days REAL DEFAULT 0,
    ease          REAL DEFAULT 2.5,
    reps          INTEGER DEFAULT 0,
    lapses        INTEGER DEFAULT 0,
    last_reviewed_at TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_cards_created  ON cards(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cards_category ON cards(category);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id INTEGER REFERENCES cards(id) ON DELETE CASCADE,
    grade TEXT NOT NULL CHECK(grade IN ('again', 'hard', 'good', 'easy')),
    reviewed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    previous_due TEXT,
    next_due TEXT,
    interval_days REAL,
    ease REAL
);
CREATE INDEX IF NOT EXISTS idx_reviews_card ON reviews(card_id);

CREATE TABLE IF NOT EXISTS obsidian_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uri TEXT NOT NULL,
    vault TEXT,
    path TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS obsidian_notes (
    note_key       TEXT PRIMARY KEY,
    vault          TEXT NOT NULL,
    path           TEXT NOT NULL,
    source_url     TEXT,
    source_title   TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_written_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_day       TEXT
);

CREATE TABLE IF NOT EXISTS note_tags (
    tag  TEXT PRIMARY KEY,
    uses INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reading_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT,
    url_key     TEXT NOT NULL UNIQUE,
    title       TEXT,
    site        TEXT,
    description TEXT,
    source      TEXT NOT NULL DEFAULT 'telegram',
    chat_id     TEXT,
    message_id  INTEGER,
    status      TEXT NOT NULL DEFAULT 'unread'
                CHECK(status IN ('unread', 'reading', 'done', 'dropped')),
    archived_at TEXT,
    archive_message_id INTEGER,
    archive_error TEXT,
    saved_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_reading_status ON reading_items(status);
CREATE INDEX IF NOT EXISTS idx_reading_saved  ON reading_items(saved_at DESC);
CREATE INDEX IF NOT EXISTS idx_reading_site   ON reading_items(site);

CREATE TABLE IF NOT EXISTS ingest_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS quiz_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url       TEXT NOT NULL,
    source_title     TEXT,
    units            TEXT NOT NULL DEFAULT '[]',
    total_units      INTEGER NOT NULL DEFAULT 0,
    asked            TEXT NOT NULL DEFAULT '[]',
    question         TEXT,
    asked_count      INTEGER NOT NULL DEFAULT 0,
    satisfied_count  INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS quiz_coverage (
    source_url      TEXT NOT NULL,
    unit_key        TEXT NOT NULL,
    times_asked     INTEGER NOT NULL DEFAULT 0,
    times_satisfied INTEGER NOT NULL DEFAULT 0,
    last_asked_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_verdict    TEXT,
    PRIMARY KEY (source_url, unit_key)
);
"""

_COLUMN_ADDITIONS = [
    "ALTER TABLE cards ADD COLUMN obsidian_path TEXT",
    "ALTER TABLE cards ADD COLUMN obsidian_url TEXT",
    "ALTER TABLE cards ADD COLUMN due_at TEXT",
    "ALTER TABLE cards ADD COLUMN interval_days REAL DEFAULT 0",
    "ALTER TABLE cards ADD COLUMN ease REAL DEFAULT 2.5",
    "ALTER TABLE cards ADD COLUMN reps INTEGER DEFAULT 0",
    "ALTER TABLE cards ADD COLUMN lapses INTEGER DEFAULT 0",
    "ALTER TABLE cards ADD COLUMN last_reviewed_at TEXT",
    "ALTER TABLE cards ADD COLUMN srs_state INTEGER",
    "ALTER TABLE cards ADD COLUMN srs_step INTEGER",
    "ALTER TABLE cards ADD COLUMN stability REAL",
    "ALTER TABLE cards ADD COLUMN difficulty REAL",
    "ALTER TABLE reviews ADD COLUMN stability REAL",
    "ALTER TABLE reviews ADD COLUMN difficulty REAL",
    "ALTER TABLE reviews ADD COLUMN srs_state INTEGER",
    "ALTER TABLE obsidian_notes ADD COLUMN last_day TEXT",
    "ALTER TABLE reading_items ADD COLUMN archived_at TEXT",
    "ALTER TABLE reading_items ADD COLUMN archive_message_id INTEGER",
    "ALTER TABLE reading_items ADD COLUMN archive_error TEXT",
]

EDITABLE_FIELDS = {"front", "back", "category", "tags", "quote", "hint"}

READING_STATUSES = ("unread", "reading", "done", "dropped")


def init_db(path: Optional[str] = None) -> None:
    """Create the schema at *path* (or the configured default)."""
    db_path = path or get_db_path()
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        for statement in _COLUMN_ADDITIONS:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                # Column already exists.
                pass
        # Must come after the column migrations above.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cards_due ON cards(due_at)")
        conn.commit()
    finally:
        conn.close()


def _db_path() -> str:
    try:
        return current_app.config.get("DB_PATH") or get_db_path()
    except RuntimeError:  # outside an application context (CLI/tests)
        return get_db_path()


def get_db() -> sqlite3.Connection:
    """Return the request-scoped connection."""
    if "flashcap_db" not in g:
        conn = sqlite3.connect(_db_path(), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        g.flashcap_db = conn
    return g.flashcap_db


def close_db(_exc: Optional[BaseException] = None) -> None:
    conn = g.pop("flashcap_db", None)
    if conn is not None:
        conn.close()


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def row_to_card(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a DB row to an API-friendly dict."""
    card = dict(row)
    try:
        tags = json.loads(card.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        tags = []
    card["tags"] = tags if isinstance(tags, list) else []
    return card


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def insert_card(data: dict[str, Any]) -> dict[str, Any]:
    """Insert a generated card and return the stored row."""
    front = str(data.get("front") or "").strip()
    back = str(data.get("back") or "").strip()
    if not front or not back:
        raise ValueError("Card needs a front and a back")

    tags = normalize_tags(data.get("tags"))
    category = normalize_category(data.get("category"), fallback="other")

    db = get_db()
    cursor = db.execute(
        """
        INSERT INTO cards (
            front, back, category, tags, source_url, source_title, source_site,
            source_author, selection, context, quote, hint, confidence, model,
            generated_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            front,
            back,
            category,
            json.dumps(tags),
            _trim(data.get("source_url"), 2000),
            _trim(data.get("source_title"), 400),
            _trim(data.get("source_site"), 200),
            _trim(data.get("source_author"), 300),
            _trim(data.get("selection"), 4000),
            _trim(data.get("context"), 12000),
            _trim(data.get("quote"), 1200),
            _trim(data.get("hint"), 500),
            _float_or_none(data.get("confidence")),
            _trim(data.get("model"), 120),
            _trim(data.get("generated_by"), 20) or "llm",
        ),
    )
    db.commit()
    return get_card(int(cursor.lastrowid))  # type: ignore[arg-type]


def update_card(card_id: int, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Apply an edit. Returns ``None`` when the card does not exist."""
    if not fields:
        return get_card(card_id)

    updates: list[str] = []
    params: list[Any] = []

    if "front" in fields:
        front = str(fields.get("front") or "").strip()
        if not front:
            raise ValueError("Front cannot be empty")
        updates.append("front = ?")
        params.append(front[:2000])

    if "back" in fields:
        back = str(fields.get("back") or "").strip()
        if not back:
            raise ValueError("Back cannot be empty")
        updates.append("back = ?")
        params.append(back[:4000])

    if "category" in fields:
        updates.append("category = ?")
        params.append(normalize_category(fields.get("category")))

    if "tags" in fields:
        updates.append("tags = ?")
        params.append(json.dumps(normalize_tags(fields.get("tags"))))

    if "quote" in fields:
        updates.append("quote = ?")
        params.append(_trim(fields.get("quote"), 1200))

    if "hint" in fields:
        updates.append("hint = ?")
        params.append(_trim(fields.get("hint"), 500))

    if not updates:
        return get_card(card_id)

    updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
    params.append(card_id)

    db = get_db()
    cursor = db.execute(
        f"UPDATE cards SET {', '.join(updates)} WHERE id = ?",  # noqa: S608 (fixed columns)
        params,
    )
    db.commit()
    if cursor.rowcount == 0:
        return None
    return get_card(card_id)


def delete_card(card_id: int) -> bool:
    db = get_db()
    cursor = db.execute("DELETE FROM cards WHERE id = ?", (card_id,))
    db.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Spaced repetition
# ---------------------------------------------------------------------------

GRADES = ("again", "hard", "good", "easy")

_RATING = {
    "again": Rating.Again,
    "hard": Rating.Hard,
    "good": Rating.Good,
    "easy": Rating.Easy,
}


def _scheduler() -> Scheduler:
    return Scheduler(
        desired_retention=get_desired_retention(),
        enable_fuzzing=get_fsrs_fuzzing(),
    )


def _parse_moment(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _to_fsrs_card(card: dict[str, Any]) -> FSRSCard:
    due = _parse_moment(card.get("due_at")) or datetime.now(timezone.utc)
    last = _parse_moment(card.get("last_reviewed_at"))
    card_id = int(card["id"])

    raw_state = card.get("srs_state")
    if raw_state is not None:
        try:
            state = State(int(raw_state))
        except (TypeError, ValueError):
            state = State.Review
        return FSRSCard(
            card_id=card_id,
            state=state,
            step=card.get("srs_step"),
            stability=card.get("stability"),
            difficulty=card.get("difficulty"),
            due=due,
            last_review=last,
        )

    # Cards reviewed before the FSRS migration have no memory state. Keep their
    # existing due date and seed a conservative review card.
    if int(card.get("reps") or 0) > 0:
        return FSRSCard(
            card_id=card_id,
            state=State.Review,
            step=None,
            stability=max(float(card.get("interval_days") or 1.0), 1.0),
            difficulty=5.0,
            due=due,
            last_review=last or due,
        )

    return FSRSCard(card_id=card_id)


def review_stats() -> dict[str, Any]:
    """Counts for the Practice page."""
    db = get_db()
    now = _iso(datetime.now(timezone.utc))
    new = db.execute(
        "SELECT COUNT(*) FROM cards WHERE reps = 0 AND due_at IS NULL"
    ).fetchone()[0]
    due = db.execute(
        "SELECT COUNT(*) FROM cards WHERE due_at IS NOT NULL AND due_at <= ?",
        (now,),
    ).fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    reviewed_today = db.execute(
        "SELECT COUNT(*) FROM reviews "
        "WHERE reviewed_at >= strftime('%Y-%m-%dT00:00:00Z', 'now')"
    ).fetchone()[0]
    next_due = db.execute(
        "SELECT MIN(due_at) FROM cards WHERE due_at IS NOT NULL AND due_at > ?",
        (now,),
    ).fetchone()[0]
    return {
        "new": new,
        "due": due,
        "available": new + due,
        "total": total,
        "reviewed_today": reviewed_today,
        "next_due_at": next_due,
        "desired_retention": get_desired_retention(),
        "scheduler": "fsrs",
    }


def next_review_card() -> Optional[dict[str, Any]]:
    """Return the next new/due card, new cards first."""
    now = _iso(datetime.now(timezone.utc))
    row = get_db().execute(
        """
        SELECT * FROM cards
        WHERE due_at IS NULL OR due_at <= ?
        ORDER BY (due_at IS NULL) DESC, COALESCE(due_at, created_at) ASC, id ASC
        LIMIT 1
        """,
        (now,),
    ).fetchone()
    return row_to_card(row) if row else None


def grade_card(card_id: int, grade: str) -> Optional[dict[str, Any]]:
    """Apply an FSRS review rating.

    Returns the updated card, or ``None`` when the card does not exist.
    """
    grade = str(grade or "").strip().lower()
    if grade not in GRADES:
        raise ValueError("grade must be one of: again, hard, good, easy")

    card = get_card(card_id)
    if card is None:
        return None

    now = datetime.now(timezone.utc)
    fsrs_card = _to_fsrs_card(card)
    next_card, _review_log = _scheduler().review_card(
        fsrs_card,
        _RATING[grade],
        review_datetime=now,
    )
    due = next_card.due.astimezone(timezone.utc)
    interval = max(0.0, (due - now).total_seconds() / 86400)
    reps = int(card.get("reps") or 0) + 1
    lapses = int(card.get("lapses") or 0) + (1 if grade == "again" else 0)
    due_at = _iso(due)
    now_iso = _iso(now)

    db = get_db()
    db.execute(
        """
        UPDATE cards
        SET due_at = ?, interval_days = ?, reps = ?, lapses = ?,
            last_reviewed_at = ?, updated_at = ?, srs_state = ?, srs_step = ?,
            stability = ?, difficulty = ?
        WHERE id = ?
        """,
        (
            due_at,
            interval,
            reps,
            lapses,
            now_iso,
            now_iso,
            int(next_card.state),
            next_card.step,
            next_card.stability,
            next_card.difficulty,
            card_id,
        ),
    )
    db.execute(
        """
        INSERT INTO reviews
            (card_id, grade, reviewed_at, previous_due, next_due, interval_days,
             ease, stability, difficulty, srs_state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            card_id,
            grade,
            now_iso,
            card.get("due_at"),
            due_at,
            interval,
            card.get("ease"),
            next_card.stability,
            next_card.difficulty,
            int(next_card.state),
        ),
    )
    db.commit()
    return get_card(card_id)

def mark_card_obsidian(card_id: int, path: str, url: str) -> None:
    """Remember the reading note a card was recorded in."""
    db = get_db()
    db.execute(
        "UPDATE cards SET obsidian_path = ?, obsidian_url = ? WHERE id = ?",
        (path, url, card_id),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Obsidian note registry (metadata only; note bodies live in Obsidian)
# ---------------------------------------------------------------------------

def get_obsidian_note(note_key: str) -> Optional[dict[str, Any]]:
    row = get_db().execute(
        "SELECT * FROM obsidian_notes WHERE note_key = ?", (note_key,)
    ).fetchone()
    return dict(row) if row else None


def upsert_obsidian_note(
    note_key: str,
    vault: str,
    path: str,
    source_url: Optional[str] = None,
    source_title: Optional[str] = None,
    last_day: Optional[str] = None,
) -> dict[str, Any]:
    db = get_db()
    db.execute(
        """
        INSERT INTO obsidian_notes
            (note_key, vault, path, source_url, source_title, last_day)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(note_key) DO UPDATE SET
            vault = excluded.vault,
            path = excluded.path,
            source_url = excluded.source_url,
            source_title = excluded.source_title,
            last_day = COALESCE(excluded.last_day, obsidian_notes.last_day),
            last_written_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
        (note_key, vault, path, source_url, source_title, last_day),
    )
    db.commit()
    return get_obsidian_note(note_key) or {}


def count_obsidian_notes() -> int:
    return get_db().execute("SELECT COUNT(*) FROM obsidian_notes").fetchone()[0]


def queue_obsidian_note(uri: str, vault: str, path: str) -> int:
    """Queue an obsidian:// URI for the Mac note agent to open."""
    db = get_db()
    cursor = db.execute(
        "INSERT INTO obsidian_queue (uri, vault, path) VALUES (?, ?, ?)",
        (uri, vault, path),
    )
    db.commit()
    return int(cursor.lastrowid)


def pending_obsidian_notes(limit: int = 50) -> list[dict[str, Any]]:
    rows = get_db().execute(
        "SELECT id, uri, vault, path, created_at FROM obsidian_queue "
        "WHERE sent_at IS NULL ORDER BY id ASC LIMIT ?",
        (max(1, min(int(limit), 200)),),
    ).fetchall()
    return [dict(row) for row in rows]


def ack_obsidian_note(queue_id: int) -> bool:
    db = get_db()
    cursor = db.execute(
        "UPDATE obsidian_queue SET sent_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
        "WHERE id = ? AND sent_at IS NULL",
        (queue_id,),
    )
    db.commit()
    return cursor.rowcount > 0


def count_pending_obsidian_notes() -> int:
    return get_db().execute(
        "SELECT COUNT(*) FROM obsidian_queue WHERE sent_at IS NULL"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_card(card_id: int) -> Optional[dict[str, Any]]:
    row = get_db().execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return row_to_card(row) if row else None


def list_cards(
    query: str = "",
    category: str = "",
    tag: str = "",
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return newest cards first, optionally filtered."""
    clauses: list[str] = []
    params: list[Any] = []

    query = (query or "").strip()
    if query:
        like = f"%{query}%"
        clauses.append(
            "(front LIKE ? OR back LIKE ? OR tags LIKE ? OR source_title LIKE ? "
            "OR source_site LIKE ? OR quote LIKE ?)"
        )
        params.extend([like] * 6)

    category = (category or "").strip()
    if category and category != "all":
        clauses.append("category = ?")
        params.append(normalize_category(category))

    sql = "SELECT * FROM cards"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 1000), 5000)))

    cards = [row_to_card(r) for r in get_db().execute(sql, params).fetchall()]

    # Tag filtering happens in Python because tags are a JSON array.
    tag = (tag or "").strip().lower()
    if tag:
        cards = [card for card in cards if tag in card["tags"]]
    return cards


def metadata() -> dict[str, Any]:
    """Return counts used by the dashboard."""
    db = get_db()
    cards = [row_to_card(r) for r in db.execute("SELECT * FROM cards").fetchall()]

    categories: dict[str, int] = {}
    tags: dict[str, int] = {}
    for card in cards:
        categories[card["category"]] = categories.get(card["category"], 0) + 1
        for tag in card["tags"]:
            tags[tag] = tags.get(tag, 0) + 1

    recent = db.execute(
        "SELECT COUNT(*) FROM cards "
        "WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-7 days')"
    ).fetchone()[0]

    obsidian_notes = db.execute("SELECT COUNT(*) FROM obsidian_notes").fetchone()[0]
    obsidian_pending = db.execute(
        "SELECT COUNT(*) FROM obsidian_queue WHERE sent_at IS NULL"
    ).fetchone()[0]

    return {
        "total": len(cards),
        "last_7_days": recent,
        "categories": dict(sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))),
        "tags": dict(sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))),
        "obsidian_notes": obsidian_notes,
        "obsidian_pending": obsidian_pending,
    }


RESERVED_TAGS = {
    "ai-explanation",
    "flashcap",
    "inbox",
    "needs-review",
    "other",
    "reading",
}


def record_note_tags(value: Any) -> None:
    """Add tags used on an annotation/note to the shared vocabulary."""
    tags = [tag for tag in normalize_tags(value) if tag not in RESERVED_TAGS]
    if not tags:
        return
    db = get_db()
    db.executemany(
        "INSERT INTO note_tags (tag, uses) VALUES (?, 1) "
        "ON CONFLICT(tag) DO UPDATE SET uses = uses + 1",
        [(tag,) for tag in tags],
    )
    db.commit()


def tag_vocabulary(limit: int = 40) -> dict[str, int]:
    """Return the shared tag vocabulary, most-used first.

    Card tags and annotation/note tags both count. The LLM may only pick from
    this list; new tags enter only when the user writes one manually.
    """
    if limit <= 0:
        return {}
    tags = {
        tag: count
        for tag, count in metadata().get("tags", {}).items()
        if tag not in RESERVED_TAGS
    }
    for row in get_db().execute("SELECT tag, uses FROM note_tags").fetchall():
        tag = row["tag"]
        if tag in RESERVED_TAGS:
            continue
        tags[tag] = tags.get(tag, 0) + int(row["uses"] or 0)
    ordered = sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(ordered[:limit])


# ---------------------------------------------------------------------------
# Reading list
# ---------------------------------------------------------------------------

def row_to_reading_item(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def get_reading_item(item_id: int) -> Optional[dict[str, Any]]:
    row = get_db().execute(
        "SELECT * FROM reading_items WHERE id = ?", (item_id,)
    ).fetchone()
    return row_to_reading_item(row) if row else None


def get_reading_item_by_key(url_key: str) -> Optional[dict[str, Any]]:
    row = get_db().execute(
        "SELECT * FROM reading_items WHERE url_key = ?", (url_key,)
    ).fetchone()
    return row_to_reading_item(row) if row else None


def upsert_reading_item(data: dict[str, Any]) -> dict[str, Any]:
    """Insert one saved item, or fill in missing fields on an identical one.

    Re-ingestion must never undo a user's status, so the conflict update only
    touches source-derived columns and only where the stored value is empty.
    """
    url_key = str(data.get("url_key") or "").strip()
    if not url_key:
        raise ValueError("Reading item needs a url_key")

    status = str(data.get("status") or "unread").strip().lower()
    if status not in READING_STATUSES:
        status = "unread"

    db = get_db()
    db.execute(
        """
        INSERT INTO reading_items (
            url, url_key, title, site, description, source, chat_id,
            message_id, status, saved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))
        ON CONFLICT(url_key) DO UPDATE SET
            title = COALESCE(NULLIF(reading_items.title, ''), excluded.title),
            site = COALESCE(NULLIF(reading_items.site, ''), excluded.site),
            description = COALESCE(
                NULLIF(reading_items.description, ''), excluded.description
            ),
            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
        (
            _trim(data.get("url"), 2000),
            url_key,
            _trim(data.get("title"), 400),
            _trim(data.get("site"), 200),
            _trim(data.get("description"), 4000),
            _trim(data.get("source"), 20) or "telegram",
            _trim(data.get("chat_id"), 40),
            _int_or_none(data.get("message_id")),
            status,
            _trim(data.get("saved_at"), 40),
        ),
    )
    db.commit()
    return get_reading_item_by_key(url_key) or {}


def update_reading_item(
    item_id: int, fields: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Apply a user edit. Returns ``None`` when the item does not exist."""
    updates: list[str] = []
    params: list[Any] = []

    if "status" in fields:
        status = str(fields.get("status") or "").strip().lower()
        if status not in READING_STATUSES:
            raise ValueError(
                "status must be one of: " + ", ".join(READING_STATUSES)
            )
        updates.append("status = ?")
        params.append(status)

    if "title" in fields:
        updates.append("title = ?")
        params.append(_trim(fields.get("title"), 400))

    if not updates:
        return get_reading_item(item_id)

    updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
    params.append(item_id)

    db = get_db()
    cursor = db.execute(
        f"UPDATE reading_items SET {', '.join(updates)} WHERE id = ?",  # noqa: S608
        params,
    )
    db.commit()
    if cursor.rowcount == 0:
        return None
    return get_reading_item(item_id)


def delete_reading_item(item_id: int) -> bool:
    db = get_db()
    cursor = db.execute("DELETE FROM reading_items WHERE id = ?", (item_id,))
    db.commit()
    return cursor.rowcount > 0


def _reading_where(
    query: str = "", status: str = "", site: str = ""
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    query = (query or "").strip()
    if query:
        like = f"%{query}%"
        clauses.append(
            "(title LIKE ? OR site LIKE ? OR description LIKE ? OR url LIKE ?)"
        )
        params.extend([like] * 4)

    status = (status or "").strip().lower()
    if status in READING_STATUSES:
        clauses.append("status = ?")
        params.append(status)

    site = (site or "").strip().lower()
    if site:
        clauses.append("LOWER(site) = ?")
        params.append(site)

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params


def list_reading_items(
    query: str = "",
    status: str = "",
    site: str = "",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return the newest saved items first, optionally filtered and paged."""
    where, params = _reading_where(query, status, site)
    sql = (
        "SELECT * FROM reading_items"
        + where
        + " ORDER BY saved_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    params.append(max(1, min(int(limit or 200), 500)))
    params.append(max(0, int(offset or 0)))

    return [row_to_reading_item(r) for r in get_db().execute(sql, params).fetchall()]


def count_reading_items(query: str = "", status: str = "", site: str = "") -> int:
    where, params = _reading_where(query, status, site)
    row = get_db().execute(
        "SELECT COUNT(*) FROM reading_items" + where, params
    ).fetchone()
    return int(row[0]) if row else 0


def mark_reading_archived(item_id: int, message_id: int) -> Optional[dict[str, Any]]:
    """Record the forwarded copy and clear any earlier archive error."""
    db = get_db()
    cursor = db.execute(
        """
        UPDATE reading_items
        SET archived_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
            archive_message_id = ?,
            archive_error = NULL
        WHERE id = ?
        """,
        (message_id, item_id),
    )
    db.commit()
    return get_reading_item(item_id) if cursor.rowcount else None


def set_reading_archive_error(
    item_id: int, message: str
) -> Optional[dict[str, Any]]:
    db = get_db()
    cursor = db.execute(
        "UPDATE reading_items SET archive_error = ? WHERE id = ?",
        (_trim(message, 500), item_id),
    )
    db.commit()
    return get_reading_item(item_id) if cursor.rowcount else None


def reading_counts() -> dict[str, int]:
    """Counts per status plus the total, for the library filters."""
    counts = {status: 0 for status in READING_STATUSES}
    for row in get_db().execute(
        "SELECT status, COUNT(*) AS n FROM reading_items GROUP BY status"
    ).fetchall():
        if row["status"] in counts:
            counts[row["status"]] = int(row["n"])
    counts["total"] = sum(
        counts[status] for status in READING_STATUSES
    )
    return counts


def reading_sites(limit: int = 30) -> list[dict[str, Any]]:
    """Sites with the most saved items, for the site filter."""
    rows = get_db().execute(
        "SELECT site, COUNT(*) AS n FROM reading_items "
        "WHERE site IS NOT NULL AND site != '' "
        "GROUP BY site ORDER BY n DESC, site ASC LIMIT ?",
        (max(1, min(int(limit or 30), 200)),),
    ).fetchall()
    return [{"site": row["site"], "count": int(row["n"])} for row in rows]


def get_state(key: str, default: str = "") -> str:
    row = get_db().execute(
        "SELECT value FROM ingest_state WHERE key = ?", (key,)
    ).fetchone()
    return str(row["value"]) if row and row["value"] is not None else default


def set_state(key: str, value: str) -> None:
    db = get_db()
    db.execute(
        """
        INSERT INTO ingest_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
        (key, value),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Quiz sessions and coverage
# ---------------------------------------------------------------------------

_QUIZ_SESSION_FIELDS = (
    "units",
    "total_units",
    "asked",
    "question",
    "asked_count",
    "satisfied_count",
)


def row_to_quiz_session(row: sqlite3.Row) -> dict[str, Any]:
    session = dict(row)
    session["units"] = _json_load(session.get("units"), [])
    session["asked"] = _json_load(session.get("asked"), [])
    session["question"] = _json_load(session.get("question"), None)
    return session


def create_quiz_session(
    source_url: str,
    source_title: str,
    units: list[dict[str, Any]],
    total_units: int,
) -> dict[str, Any]:
    db = get_db()
    cursor = db.execute(
        """
        INSERT INTO quiz_sessions (source_url, source_title, units, total_units)
        VALUES (?, ?, ?, ?)
        """,
        (
            source_url,
            source_title or None,
            json.dumps(units),
            max(0, min(int(total_units or 0), 5000)),
        ),
    )
    db.commit()
    return get_quiz_session(int(cursor.lastrowid)) or {}


def get_quiz_session(session_id: int) -> Optional[dict[str, Any]]:
    row = get_db().execute(
        "SELECT * FROM quiz_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    return row_to_quiz_session(row) if row else None


def update_quiz_session(
    session_id: int, fields: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Apply a session update. ``units``, ``asked`` and ``question`` are JSON."""
    updates: list[str] = []
    params: list[Any] = []

    for key in _QUIZ_SESSION_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if key in ("units", "asked", "question"):
            value = json.dumps(value)
        elif key == "total_units":
            value = max(0, min(int(value or 0), 5000))
        else:
            value = max(0, int(value or 0))
        updates.append(f"{key} = ?")
        params.append(value)

    if not updates:
        return get_quiz_session(session_id)

    updates.append("updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
    params.append(session_id)
    db = get_db()
    cursor = db.execute(
        f"UPDATE quiz_sessions SET {', '.join(updates)} WHERE id = ?",  # noqa: S608
        params,
    )
    db.commit()
    if cursor.rowcount == 0:
        return None
    return get_quiz_session(session_id)


def record_quiz_coverage(
    source_url: str, unit_key: str, verdict: str | None = None
) -> None:
    """Remember that a passage was shown a question.

    A passage counts as quizzed when its question is opened, so abandoning a
    panel still moves the next session on. The verdict lands when the question
    resolves: ``satisfactory`` or ``skipped``.
    """
    db = get_db()
    if verdict is None:
        db.execute(
            """
            INSERT INTO quiz_coverage (source_url, unit_key, times_asked)
            VALUES (?, ?, 1)
            ON CONFLICT(source_url, unit_key) DO UPDATE SET
                times_asked = quiz_coverage.times_asked + 1,
                last_asked_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (source_url, unit_key),
        )
    else:
        db.execute(
            """
            INSERT INTO quiz_coverage
                (source_url, unit_key, times_asked, times_satisfied, last_verdict)
            VALUES (?, ?, 0, ?, ?)
            ON CONFLICT(source_url, unit_key) DO UPDATE SET
                times_satisfied = quiz_coverage.times_satisfied + excluded.times_satisfied,
                last_asked_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                last_verdict = excluded.last_verdict
            """,
            (
                source_url,
                unit_key,
                1 if verdict == "satisfactory" else 0,
                verdict,
            ),
        )
    db.commit()


def quiz_coverage(source_url: str) -> dict[str, dict[str, Any]]:
    rows = get_db().execute(
        "SELECT unit_key, times_asked, times_satisfied, last_asked_at, last_verdict "
        "FROM quiz_coverage WHERE source_url = ?",
        (source_url,),
    ).fetchall()
    return {
        row["unit_key"]: {
            "times_asked": int(row["times_asked"] or 0),
            "times_satisfied": int(row["times_satisfied"] or 0),
            "last_asked_at": row["last_asked_at"] or "",
            "last_verdict": row["last_verdict"],
        }
        for row in rows
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _trim(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _json_load(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return None


def _iso(moment: datetime) -> str:
    return (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
