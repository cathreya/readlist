"""Tests for the reading library: store, legacy import, and Telegram ingest."""

from __future__ import annotations

import sqlite3

import pytest

from capture import library, server, store


@pytest.fixture()
def app(tmp_path):
    application = server.create_app(str(tmp_path / "cards.db"))
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    with app.test_client() as test_client:
        yield test_client


def make_legacy_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items ("
        "id INTEGER PRIMARY KEY, url TEXT, title TEXT, domain TEXT, "
        "description TEXT, status TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO items (id, url, title, domain, description, status, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def test_upsert_keeps_user_status_and_fills_empty_fields(app):
    with app.app_context():
        first = store.upsert_reading_item(
            {"url": "https://example.com/a", "url_key": "https://example.com/a"}
        )
        assert first["status"] == "unread"
        store.update_reading_item(first["id"], {"status": "done"})

        again = store.upsert_reading_item(
            {
                "url": "https://example.com/a",
                "url_key": "https://example.com/a",
                "title": "A story",
                "description": "Saved from Telegram",
            }
        )
        assert again["id"] == first["id"]
        assert again["status"] == "done"
        assert again["title"] == "A story"
        assert again["description"] == "Saved from Telegram"


def test_counts_sites_and_filters(app):
    with app.app_context():
        for key, site, status in [
            ("https://a.example/1", "a.example", "unread"),
            ("https://a.example/2", "a.example", "done"),
            ("https://b.example/1", "b.example", "unread"),
        ]:
            item = store.upsert_reading_item(
                {"url": key, "url_key": key, "site": site, "title": key}
            )
            store.update_reading_item(item["id"], {"status": status})

        assert store.reading_counts() == {
            "unread": 2,
            "reading": 0,
            "done": 1,
            "dropped": 0,
            "total": 3,
        }
        assert store.reading_sites()[0] == {"site": "a.example", "count": 2}
        assert len(store.list_reading_items(status="done")) == 1
        assert len(store.list_reading_items(site="b.example")) == 1
        assert len(store.list_reading_items(query="b.example")) == 1


def test_update_rejects_unknown_status(app):
    with app.app_context():
        item = store.upsert_reading_item(
            {"url": "https://example.com/x", "url_key": "https://example.com/x"}
        )
        with pytest.raises(ValueError):
            store.update_reading_item(item["id"], {"status": "archived"})


# ---------------------------------------------------------------------------
# Legacy import
# ---------------------------------------------------------------------------

def test_legacy_import_maps_status_and_notes(app, tmp_path):
    legacy = tmp_path / "lecture_today.db"
    make_legacy_db(
        legacy,
        [
            (
                1,
                "https://example.com/x?utm_source=tg",
                None,
                "example.com",
                "read this",
                "unread",
                "2026-05-09 02:06:30",
            ),
            (
                2,
                "https://en.wikipedia.org/wiki/Jugurtha",
                None,
                "wikipedia",
                "https://en.wikipedia.org/wiki/Jugurtha",
                "in_progress",
                "2026-05-09 02:06:30",
            ),
            (
                3,
                "note://note/123",
                "Carmen",
                None,
                "Carmen",
                "skipped",
                "2026-05-09 02:06:30",
            ),
        ],
    )

    with app.app_context():
        assert library.import_legacy(str(legacy)) == {
            "seen": 3,
            "added": 3,
            "total": 3,
        }
        items = {item["url_key"]: item for item in store.list_reading_items()}
        assert items["https://example.com/x"]["status"] == "unread"
        assert items["https://example.com/x"]["saved_at"] == "2026-05-09T02:06:30Z"
        assert (
            items["https://en.wikipedia.org/wiki/Jugurtha"]["status"] == "reading"
        )
        assert items["legacy:note:3"]["url"] is None
        assert items["legacy:note:3"]["status"] == "dropped"
        assert items["legacy:note:3"]["title"] == "Carmen"

        assert library.import_legacy(str(legacy))["added"] == 0


def test_legacy_import_rejects_missing_database(app, tmp_path):
    with app.app_context():
        with pytest.raises(FileNotFoundError):
            library.import_legacy(str(tmp_path / "nope.db"))
