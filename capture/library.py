"""The reading library: identity, migration, and one-off import.

The library lives in Flashcap's own database. Telegram and manual entries are
the live sources; ``lecture_today.db`` is read once to seed the backlog and is
never written.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import store
from .obsidian import normalize_url

SOURCE_IMPORT = "import"
SOURCE_TELEGRAM = "telegram"
SOURCE_MANUAL = "manual"

# The tutor's six statuses collapse onto the library's four.
LEGACY_STATUS = {
    "unread": "unread",
    "in_progress": "reading",
    "completed": "done",
    "skipped": "dropped",
    "dead": "dropped",
    "error": "dropped",
}


def canonical_key(url: object) -> str:
    """Return the identity of a saved link, or "" when it is not a web URL."""
    return normalize_url(url)


def host_of(url: object) -> str:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def legacy_saved_at(value: object) -> str:
    """Turn the old app's ``YYYY-MM-DD HH:MM:SS`` into ISO UTC."""
    raw = str(value or "").strip()
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        moment = datetime.fromisoformat(raw.replace(" ", "T"))
    except ValueError:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def legacy_rows(legacy_db_path: str) -> list[dict]:
    """Read the old app's items as library rows. Opens the file read-only."""
    if not os.path.isfile(legacy_db_path):
        raise FileNotFoundError(f"No Lecture Today database at {legacy_db_path}")

    conn = sqlite3.connect(f"file:{legacy_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, url, title, domain, description, status, created_at "
            "FROM items ORDER BY id ASC"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"{legacy_db_path} has no items table") from exc
    finally:
        conn.close()

    items: list[dict] = []
    for row in rows:
        url = str(row["url"] or "").strip()
        is_note = url.startswith("note://")
        key = (
            f"legacy:note:{row['id']}"
            if is_note
            else canonical_key(url) or f"legacy:url:{row['id']}"
        )
        title = str(row["title"] or "").strip() or None
        description = str(row["description"] or "").strip()
        items.append(
            {
                "url": None if is_note else url,
                "url_key": key,
                "title": title,
                "site": str(row["domain"] or "").strip() or host_of(url) or None,
                "description": description or None,
                "source": SOURCE_IMPORT,
                "saved_at": legacy_saved_at(row["created_at"]),
                "status": LEGACY_STATUS.get(str(row["status"] or ""), "unread"),
            }
        )
    return items


def import_legacy(legacy_db_path: str) -> dict[str, int]:
    """Seed the library from ``lecture_today.db``. Safe to run repeatedly."""
    rows = legacy_rows(legacy_db_path)
    before = store.reading_counts()["total"]
    for item in rows:
        store.upsert_reading_item(item)
    after = store.reading_counts()["total"]
    return {"seen": len(rows), "added": after - before, "total": after}
