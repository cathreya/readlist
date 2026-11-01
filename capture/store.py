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
]

EDITABLE_FIELDS = {"front", "back", "category", "tags", "quote", "hint"}


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
# Helpers
# ---------------------------------------------------------------------------

def _trim(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


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
