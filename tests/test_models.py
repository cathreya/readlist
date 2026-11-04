"""Tests for models.py -- database schema, connection, and utility functions."""

import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

# Path setup so that `from app.models import ...` works.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(PROJECT_ROOT, "app")
for _p in (PROJECT_ROOT, APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.models import compute_resurface_date


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_db_path(tmp_path):
    """Temporarily redirect models.DB_PATH to a temp file."""
    import app.models as models
    original = models.DB_PATH
    models.DB_PATH = os.path.join(tmp_path, "test.db")
    yield
    models.DB_PATH = original


@pytest.fixture
def db(patched_db_path):
    """Return an open connection to a freshly-initialised temporary DB."""
    from app.models import init_db, get_db
    init_db()
    conn = get_db()
    yield conn
    conn.close()


# ===================================================================
# TestInitDb
# ===================================================================

class TestInitDb:
    def test_creates_all_expected_tables(self, db):
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in tables}
        expected = {"items", "item_prerequisites", "sessions",
                    "skips", "card_queue", "classification_runs"}
        missing = expected - names
        assert not missing, f"Missing tables: {missing}"

    def test_items_table_has_all_columns(self, db):
        cols = db.execute("PRAGMA table_info(items)").fetchall()
        col_names = {r["name"] for r in cols}
        expected = {
            "id", "url", "title", "domain", "content_type", "description",
            "estimated_minutes", "difficulty", "topic", "blurb",
            "chunk_total", "chunk_index", "parent_item_id",
            "local_file_path", "warmup", "guided_questions",
            "status", "error_reason", "created_at",
        }
        missing = expected - col_names
        assert not missing, f"Missing columns in items: {missing}"

    def test_init_db_is_idempotent(self, tmp_path):
        import app.models as models
        orig = models.DB_PATH
        models.DB_PATH = os.path.join(tmp_path, "idempotent.db")
        models.init_db()
        models.init_db()  # second call must not raise
        models.DB_PATH = orig

    def test_item_prerequisites_table(self, db):
        cols = db.execute("PRAGMA table_info(item_prerequisites)").fetchall()
        col_names = {r["name"] for r in cols}
        for c in ("item_id", "prereq_id", "confidence"):
            assert c in col_names, f"Missing column: {c}"

    def test_sessions_table(self, db):
        cols = db.execute("PRAGMA table_info(sessions)").fetchall()
        col_names = {r["name"] for r in cols}
        for c in ("item_id", "lecture_title", "completed_at", "exit_note", "session_duration_minutes"):
            assert c in col_names

    def test_skips_table(self, db):
        cols = db.execute("PRAGMA table_info(skips)").fetchall()
        col_names = {r["name"] for r in cols}
        for c in ("item_id", "reason", "skipped_at", "resurface_after"):
            assert c in col_names

    def test_card_queue_table(self, db):
        cols = db.execute("PRAGMA table_info(card_queue)").fetchall()
        col_names = {r["name"] for r in cols}
        for c in ("item_id", "session_id", "front", "back", "flushed"):
            assert c in col_names

    def test_classification_runs_table(self, db):
        cols = db.execute("PRAGMA table_info(classification_runs)").fetchall()
        col_names = {r["name"] for r in cols}
        for c in ("run_at", "items_processed", "model_used"):
            assert c in col_names


# ===================================================================
# TestGetDb
# ===================================================================

class TestGetDb:
    def test_returns_live_connection(self, tmp_path):
        import app.models as models
        orig = models.DB_PATH
        models.DB_PATH = os.path.join(tmp_path, "live.db")
        models.init_db()
        conn = models.get_db()
        assert conn is not None
        row = conn.execute("SELECT 1 AS v").fetchone()
        assert row["v"] == 1
        conn.close()
        models.DB_PATH = orig

    def test_uses_sqlite3_row_factory(self, tmp_path):
        import app.models as models
        orig = models.DB_PATH
        models.DB_PATH = os.path.join(tmp_path, "row_factory.db")
        models.init_db()
        conn = models.get_db()
        assert conn.row_factory is sqlite3.Row
        conn.close()
        models.DB_PATH = orig

    def test_has_wal_journal_mode(self, tmp_path):
        import app.models as models
        orig = models.DB_PATH
        models.DB_PATH = os.path.join(tmp_path, "wal.db")
        models.init_db()
        conn = models.get_db()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
        conn.close()
        models.DB_PATH = orig

    def test_has_foreign_keys_enabled(self, tmp_path):
        import app.models as models
        orig = models.DB_PATH
        models.DB_PATH = os.path.join(tmp_path, "fk.db")
        models.init_db()
        conn = models.get_db()
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        conn.close()
        models.DB_PATH = orig


# ===================================================================
# TestComputeResurfaceDate
# ===================================================================

class TestComputeResurfaceDate:
    def test_too_hard_returns_14_days(self):
        dt = datetime.fromisoformat(compute_resurface_date("too_hard"))
        diff = (dt - datetime.utcnow()).days
        assert 13 <= diff <= 15, f"Expected ~14 days, got {diff}"

    def test_not_right_now_returns_7_days(self):
        dt = datetime.fromisoformat(compute_resurface_date("not_right_now"))
        diff = (dt - datetime.utcnow()).days
        assert 6 <= diff <= 8, f"Expected ~7 days, got {diff}"

    def test_not_interested_returns_30_days(self):
        dt = datetime.fromisoformat(compute_resurface_date("not_interested"))
        diff = (dt - datetime.utcnow()).days
        assert 29 <= diff <= 31, f"Expected ~30 days, got {diff}"

    def test_unknown_reason_defaults_to_7_days(self):
        dt = datetime.fromisoformat(compute_resurface_date("bogus_reason"))
        diff = (dt - datetime.utcnow()).days
        assert 6 <= diff <= 8

    def test_empty_reason_defaults_to_7_days(self):
        dt = datetime.fromisoformat(compute_resurface_date(""))
        diff = (dt - datetime.utcnow()).days
        assert 6 <= diff <= 8


# ===================================================================
# TestItemsCRUD
# ===================================================================

class TestItemsCRUD:
    def test_insert_and_query(self, db):
        db.execute(
            "INSERT INTO items (url, title, domain, content_type, description, "
            "estimated_minutes, difficulty, topic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("https://example.com", "Example", "example.com", "article",
             "An example description", 10, 3, "testing"),
        )
        db.commit()
        row = db.execute("SELECT * FROM items WHERE url=?", ("https://example.com",)).fetchone()
        assert row is not None
        assert row["title"] == "Example"
        assert row["domain"] == "example.com"
        assert row["content_type"] == "article"
        assert row["difficulty"] == 3
        assert row["estimated_minutes"] == 10
        assert row["topic"] == "testing"

    def test_unique_url_constraint(self, db):
        db.execute("INSERT INTO items (url) VALUES (?)", ("https://example.com",))
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO items (url) VALUES (?)", ("https://example.com",))

    def test_url_case_sensitive_uniqueness(self, db):
        """Without a lower() index, different casing yields distinct rows."""
        db.execute("INSERT INTO items (url) VALUES (?)", ("https://Example.com/A",))
        db.commit()
        db.execute("INSERT INTO items (url) VALUES (?)", ("https://example.com/a",))
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2

    def test_update_status(self, db):
        db.execute("INSERT INTO items (url, status) VALUES (?, ?)",
                   ("https://example.com", "unread"))
        db.commit()
        db.execute("UPDATE items SET status=? WHERE url=?",
                   ("completed", "https://example.com"))
        db.commit()
        row = db.execute("SELECT status FROM items WHERE url=?",
                         ("https://example.com",)).fetchone()
        assert row["status"] == "completed"

    def test_difficulty_check_constraint_invalid(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO items (url, difficulty) VALUES (?, ?)",
                       ("https://example.com", 6))

    def test_difficulty_check_constraint_valid(self, db):
        for d in range(1, 6):
            db.execute("INSERT INTO items (url, difficulty) VALUES (?, ?)",
                       (f"https://example.com/{d}", d))
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 5

    def test_default_status_is_unread(self, db):
        db.execute("INSERT INTO items (url) VALUES (?)", ("https://example.com",))
        db.commit()
        row = db.execute("SELECT status FROM items WHERE url=?",
                         ("https://example.com",)).fetchone()
        assert row["status"] == "unread"

    def test_default_created_at_is_populated(self, db):
        db.execute("INSERT INTO items (url) VALUES (?)", ("https://example.com",))
        db.commit()
        val = db.execute("SELECT created_at FROM items WHERE url=?",
                         ("https://example.com",)).fetchone()[0]
        assert val is not None

    def test_delete_item(self, db):
        db.execute("INSERT INTO items (url) VALUES (?)", ("https://example.com",))
        db.commit()
        db.execute("DELETE FROM items WHERE url=?", ("https://example.com",))
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0

    def test_insert_multiple_items(self, db):
        for i in range(10):
            db.execute("INSERT INTO items (url, title) VALUES (?, ?)",
                       (f"https://example.com/{i}", f"Item {i}"))
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 10

    def test_null_url_allowed(self, db):
        """url column is NOT NULL via the schema, so this should fail."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO items (id, title) VALUES (1, 'No URL')")


# ===================================================================
# TestForeignKeyCascade (basic referential integrity)
# ===================================================================

class TestForeignKeys:
    def test_item_prerequisites_references_items(self, db):
        db.execute("INSERT INTO items (id, url) VALUES (1, 'https://a.com')")
        db.execute("INSERT INTO items (id, url) VALUES (2, 'https://b.com')")
        db.execute("INSERT INTO item_prerequisites (item_id, prereq_id, confidence) "
                   "VALUES (2, 1, 0.9)")
        db.commit()
        row = db.execute("SELECT * FROM item_prerequisites").fetchone()
        assert row["item_id"] == 2
        assert row["prereq_id"] == 1
        assert abs(row["confidence"] - 0.9) < 0.01

    def test_prerequisite_confidence_out_of_range(self, db):
        db.execute("INSERT INTO items (id, url) VALUES (1, 'https://a.com')")
        db.execute("INSERT INTO items (id, url) VALUES (2, 'https://b.com')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO item_prerequisites (item_id, prereq_id, confidence) "
                       "VALUES (2, 1, 1.5)")

    def test_session_orphan_blocked(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO sessions (item_id) VALUES (999)")

    def test_skip_orphan_blocked(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO skips (item_id, reason) VALUES (999, 'too_hard')")

    def test_skip_reason_check_constraint(self, db):
        db.execute("INSERT INTO items (id, url) VALUES (1, 'https://a.com')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO skips (item_id, reason) VALUES (1, 'invalid_reason')")

    def test_valid_skip_reasons(self, db):
        db.execute("INSERT INTO items (id, url) VALUES (1, 'https://a.com')")
        for reason in ("too_hard", "not_right_now", "not_interested", "too_long"):
            db.execute("INSERT INTO skips (item_id, reason) VALUES (?, ?)", (1, reason))
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM skips").fetchone()[0] == 4


# ===================================================================
# TestCardQueue
# ===================================================================

class TestCardQueue:
    def test_insert_and_query_cards(self, db):
        db.execute("INSERT INTO items (id, url) VALUES (1, 'https://a.com')")
        db.execute("INSERT INTO sessions (id, item_id) VALUES (10, 1)")
        db.execute("INSERT INTO card_queue (item_id, session_id, front, back) "
                   "VALUES (1, 10, 'Front text', 'Back text')")
        db.commit()
        row = db.execute("SELECT * FROM card_queue").fetchone()
        assert row["front"] == "Front text"
        assert row["back"] == "Back text"
        assert row["flushed"] == 0

    def test_default_flushed_is_zero(self, db):
        db.execute("INSERT INTO items (id, url) VALUES (1, 'https://a.com')")
        db.execute("INSERT INTO sessions (id, item_id) VALUES (1, 1)")
        db.execute("INSERT INTO card_queue (item_id, session_id, front, back) "
                   "VALUES (1, 1, 'F', 'B')")
        db.commit()
        assert db.execute("SELECT flushed FROM card_queue").fetchone()[0] == 0

    def test_update_flushed(self, db):
        db.execute("INSERT INTO items (id, url) VALUES (1, 'https://a.com')")
        db.execute("INSERT INTO sessions (id, item_id) VALUES (1, 1)")
        db.execute("INSERT INTO card_queue (item_id, session_id, front, back) "
                   "VALUES (1, 1, 'F', 'B')")
        db.commit()
        db.execute("UPDATE card_queue SET flushed=1 WHERE id=1")
        db.commit()
        assert db.execute("SELECT flushed FROM card_queue").fetchone()[0] == 1
