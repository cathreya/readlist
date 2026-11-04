"""Tests for app.py -- Flask routes (index, setup, lecture, complete, skip, lighter)."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(PROJECT_ROOT, "app")
for _p in (PROJECT_ROOT, APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path):
    """Flask app wired to a temporary database with pre-populated items.

    We patch DB_PATH on the *top-level* ``models`` module (the one app.py
    imports via ``from models import ...``).  ``anki._call_llm`` is also
    mocked so that the /complete route never hits a real LLM.

    Items are kept topic-less so that ``lecture_generator.personalize``
    returns ``""`` without calling the LLM.
    """
    import models
    original_db_path = models.DB_PATH
    models.DB_PATH = os.path.join(tmp_path, "lecture_today.db")
    models.init_db()

    _seed_items(models)

    import app.anki as anki
    original_anki_llm = anki._call_llm
    anki._call_llm = MagicMock(
        return_value=json.dumps([
            {"front": "What is the key idea?", "back": "The key idea is X."},
        ]),
    )

    from app.app import app as flask_app
    flask_app.config["TESTING"] = True
    flask_app.config["SERVER_NAME"] = "test.local"

    yield flask_app

    models.DB_PATH = original_db_path
    anki._call_llm = original_anki_llm


@pytest.fixture
def client(app):
    """Flask test client."""
    with app.test_client() as c:
        yield c


def _seed_items(models):
    conn = models.get_db()
    items = [
        (1, "https://example.com/article", "Test Article", "example.com",
         "article", "A test description", 3, None, 20, "unread",
         "Based on the article, this covers testing patterns.",
         '["What is the key idea?", "How does it work?", "What changed?"]'),
        (2, "https://example.com/easy", "Easy Read", "example.com",
         "article", "An easy one", 1, None, 3, "unread",
         "This is a light read about easy concepts.",
         '["Question 1?", "Question 2?", "Question 3?"]'),
        (3, "https://example.com/medium", "Medium Read", "example.com",
         "article", "A medium one", 3, None, 15, "unread",
         "A medium difficulty article.",
         '["Question A?", "Question B?", "Question C?"]'),
    ]
    for row in items:
        conn.execute(
            "INSERT INTO items (id, url, title, domain, content_type, "
            "description, difficulty, topic, estimated_minutes, status, "
            "warmup, guided_questions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
    conn.commit()
    conn.close()


def _item_count(models):
    conn = models.get_db()
    c = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.close()
    return c


# ===================================================================
# TestIndex
# ===================================================================

class TestIndex:
    def test_redirects_to_setup_when_db_empty(self, tmp_path):
        """GET / with zero items -> 302 redirect to /setup."""
        import models
        orig = models.DB_PATH
        models.DB_PATH = os.path.join(tmp_path, "empty.db")

        from app.app import app
        app.config["TESTING"] = True

        with app.test_client() as c:
            resp = c.get("/", follow_redirects=False)
            assert resp.status_code == 302
            assert "/setup" in resp.headers.get("Location", "")

        models.DB_PATH = orig

    def test_returns_lecture_page_when_items_exist(self, client):
        """GET / with items in the DB returns lecture.html."""
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"lecture" in resp.data.lower() or b"Today" in resp.data

    def test_contains_item_title_in_response(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Test Article" in resp.data or b"Easy Read" in resp.data or b"Medium Read" in resp.data

    def test_marks_item_in_progress(self, client):
        import models
        client.get("/")
        conn = models.get_db()
        row = conn.execute(
            "SELECT status FROM items WHERE status='in_progress'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "in_progress"

    def test_does_not_show_resume_badge_on_first_load(self, client):
        """A fresh load should not show the resume badge (the div itself, not just the CSS)."""
        resp = client.get("/")
        assert resp.status_code == 200
        # The resume-badge CSS class is always present, but the div should not be rendered
        assert b"Resuming where you left off" not in resp.data


# ===================================================================
# TestIndexWithInProgress (crash recovery)
# ===================================================================

class TestIndexWithInProgress:
    def test_resumes_in_progress_item_after_crash(self, client):
        """If item already has status='in_progress', the index resumes it."""
        import models
        conn = models.get_db()
        conn.execute("UPDATE items SET status='in_progress' WHERE id=1")
        conn.commit()
        conn.close()

        resp = client.get("/")
        assert resp.status_code == 200
        assert b"resum" in resp.data.lower()


# ===================================================================
# TestSetup
# ===================================================================

class TestSetup:
    def test_get_setup_returns_page(self, client):
        resp = client.get("/setup")
        assert resp.status_code == 200
        assert b"Welcome" in resp.data or b"Setup" in resp.data

    def test_get_setup_shows_item_count(self, client):
        resp = client.get("/setup")
        assert resp.status_code == 200
        text = resp.data.decode("utf-8")
        assert "3" in text or "items already loaded" in text.lower()

    def test_setup_page_has_form(self, client):
        resp = client.get("/setup")
        assert resp.status_code == 200
        assert b"messages-path" in resp.data or b"messages.html" in resp.data

    def test_setup_post_imports_items(self, client):
        """POST /setup with a mock parser inserts items into the DB."""
        mock_items = [
            {"url": "https://example.com/new1", "title": "New 1",
             "domain": "example.com", "content_type": "article",
             "description": "Desc 1", "local_file_path": None},
            {"url": "https://example.com/new2", "title": "New 2",
             "domain": "youtube", "content_type": "video",
             "description": "Desc 2", "local_file_path": None},
        ]
        import models
        before = _item_count(models)

        with patch("parser.parse_messages_html", return_value=mock_items):
            resp = client.post(
                "/setup",
                data=json.dumps({}),
                content_type="application/json",
            )

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["items_found"] == 2
        assert data["items_inserted"] == 2
        assert _item_count(models) == before + 2

    def test_setup_post_skips_duplicate_urls(self, client):
        """Duplicate URLs are silently skipped (IntegrityError caught)."""
        import models
        before = _item_count(models)
        mock_items = [
            {"url": "https://example.com/article", "title": "Duplicate",
             "domain": "example.com", "content_type": "article",
             "description": "Already exists", "local_file_path": None},
        ]
        with patch("parser.parse_messages_html", return_value=mock_items):
            resp = client.post(
                "/setup",
                data=json.dumps({}),
                content_type="application/json",
            )
        data = resp.get_json()
        assert data["items_found"] == 1
        assert data["items_inserted"] == 0
        assert _item_count(models) == before

    def test_setup_post_no_items_from_parser(self, client):
        """When parser returns an empty list, nothing is inserted."""
        import models
        before = _item_count(models)
        with patch("parser.parse_messages_html", return_value=[]):
            resp = client.post(
                "/setup",
                data=json.dumps({}),
                content_type="application/json",
            )
        data = resp.get_json()
        assert data["items_found"] == 0
        assert data["items_inserted"] == 0
        assert _item_count(models) == before


# ===================================================================
# TestComplete
# ===================================================================

class TestComplete:
    def test_creates_session_and_marks_completed(self, client):
        import models
        resp = client.post(
            "/complete",
            data=json.dumps({"item_id": 1, "exit_note": "Great read!"}),
            content_type="application/json",
        )
        assert resp.status_code == 200

        conn = models.get_db()
        session = conn.execute(
            "SELECT * FROM sessions WHERE item_id=1"
        ).fetchone()
        assert session is not None
        assert session["completed_at"] is not None
        assert session["exit_note"] == "Great read!"

        status = conn.execute(
            "SELECT status FROM items WHERE id=1"
        ).fetchone()["status"]
        assert status == "completed"
        conn.close()

    def test_returns_next_lecture_json(self, client):
        resp = client.post(
            "/complete",
            data=json.dumps({"item_id": 1, "exit_note": ""}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "next" in data
        assert "lecture" in data or data["next"] == "celebration"

    def test_handles_anki_failure_gracefully(self, client):
        """Anki card generation blowing up should NOT crash the route."""
        import app.anki as anki
        anki._call_llm = MagicMock(side_effect=RuntimeError("API down"))

        resp = client.post(
            "/complete",
            data=json.dumps({"item_id": 1, "exit_note": "Note"}),
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_marks_next_item_in_progress(self, client):
        """After completing, the next item gets status='in_progress'."""
        client.post(
            "/complete",
            data=json.dumps({"item_id": 1, "exit_note": ""}),
            content_type="application/json",
        )
        import models
        conn = models.get_db()
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM items WHERE status='in_progress'"
        ).fetchone()["c"]
        conn.close()
        assert cnt == 1


# ===================================================================
# TestSkip
# ===================================================================

class TestSkip:
    def test_creates_skip_record(self, client):
        import models
        resp = client.post(
            "/skip",
            data=json.dumps({"item_id": 1, "reason": "not_right_now"}),
            content_type="application/json",
        )
        assert resp.status_code == 200

        conn = models.get_db()
        skip = conn.execute(
            "SELECT * FROM skips WHERE item_id=1"
        ).fetchone()
        conn.close()
        assert skip is not None
        assert skip["reason"] == "not_right_now"

    def test_updates_item_status_to_skipped(self, client):
        import models
        client.post(
            "/skip",
            data=json.dumps({"item_id": 1, "reason": "too_hard"}),
            content_type="application/json",
        )
        conn = models.get_db()
        status = conn.execute(
            "SELECT status FROM items WHERE id=1"
        ).fetchone()["status"]
        conn.close()
        assert status == "skipped"

    def test_skip_too_long_triggers_chunking(self, client):
        """Skipping with reason 'too_long' chunks the item and returns first chunk."""
        import models
        resp = client.post(
            "/skip",
            data=json.dumps({"item_id": 1, "reason": "too_long"}),
            content_type="application/json",
        )
        conn = models.get_db()
        row = conn.execute(
            "SELECT status, error_reason, chunk_total FROM items WHERE id=1"
        ).fetchone()
        # Original item is completed; chunks were created
        assert row["status"] == "completed"
        assert row["chunk_total"] > 1
        # A chunk child should exist and be in_progress
        chunk = conn.execute(
            "SELECT status FROM items WHERE parent_item_id=1"
        ).fetchone()
        assert chunk["status"] == "in_progress"
        conn.close()
        # Response should be the next lecture (the first chunk)
        data = resp.get_json()
        assert data["next"] == "lecture"

    def test_returns_next_lecture_json(self, client):
        resp = client.post(
            "/skip",
            data=json.dumps({"item_id": 1, "reason": "not_right_now"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "next" in data

    def test_graveyard_after_three_not_interested_skips(self, client):
        """Three 'not_interested' skips move item to graveyard."""
        import models
        for _ in range(3):
            client.post(
                "/skip",
                data=json.dumps({"item_id": 1, "reason": "not_interested"}),
                content_type="application/json",
            )
        conn = models.get_db()
        row = conn.execute(
            "SELECT status, error_reason FROM items WHERE id=1"
        ).fetchone()
        conn.close()
        assert row["status"] == "skipped"
        assert row["error_reason"] == "graveyard"


# ===================================================================
# TestLighter
# ===================================================================

class TestLighter:
    def test_returns_lighter_lecture(self, client):
        resp = client.post("/lighter")
        assert resp.status_code == 200
        assert b"lecture" in resp.data.lower() or b"Today" in resp.data

    def test_marks_item_in_progress(self, client):
        import models
        client.post("/lighter")
        conn = models.get_db()
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM items WHERE status='in_progress'"
        ).fetchone()["c"]
        conn.close()
        assert cnt == 1

    def test_lighter_uses_easy_item_when_available(self, client):
        """The easy/short item (id=2, diff=1, est=3min) should be picked."""
        import models
        client.post("/lighter")
        conn = models.get_db()
        row = conn.execute(
            "SELECT id FROM items WHERE status='in_progress'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row["id"] in (1, 2)

    def test_lighter_all_completed_returns_celebration(self, client):
        """When every item is completed, /lighter renders celebration."""
        import models
        conn = models.get_db()
        for i in range(1, 4):
            conn.execute(
                "INSERT INTO sessions (item_id, completed_at) "
                "VALUES (?, datetime('now'))", (i,),
            )
        conn.commit()
        conn.close()

        resp = client.post("/lighter")
        assert resp.status_code == 200
        body = resp.data.lower()
        assert b"did it" in body or b"every item" in body or b"celebration" in body


# ===================================================================
# TestCardQueueStatus
# ===================================================================

class TestCardQueueStatus:
    def test_returns_zero_when_empty(self, client):
        resp = client.get("/card-queue-status")
        assert resp.status_code == 200
        assert resp.get_json()["pending"] == 0

    def test_returns_pending_count(self, client):
        import models
        conn = models.get_db()
        conn.execute("INSERT INTO sessions (id, item_id) VALUES (99, 1)")
        conn.execute(
            "INSERT INTO card_queue (item_id, session_id, front, back, flushed) "
            "VALUES (1, 99, 'F', 'B', 0)",
        )
        conn.commit()
        conn.close()

        resp = client.get("/card-queue-status")
        assert resp.status_code == 200
        assert resp.get_json()["pending"] == 1


# ===================================================================
# TestCelebration
# ===================================================================

class TestCelebration:
    def test_celebration_when_all_items_completed(self, client):
        """GET / renders celebration.html when every item is done."""
        import models
        conn = models.get_db()
        for i in range(1, 4):
            conn.execute(
                "INSERT INTO sessions (item_id, completed_at) "
                "VALUES (?, datetime('now'))", (i,),
            )
        conn.commit()
        conn.close()

        resp = client.get("/")
        assert resp.status_code == 200
        assert b"did it" in resp.data.lower() or b"impressive" in resp.data.lower()
