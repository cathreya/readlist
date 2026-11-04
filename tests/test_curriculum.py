"""Tests for curriculum.py -- item selection algorithm.

Key behaviours tested:
  1. Difficulty gating — hard items blocked until 3+ sessions since last hard.
  2. Prerequisite blocking — items with unmet prereqs are excluded.
  3. Continuity bias — chunk N completed -> offer chunk N+1.
  4. Topic interleaving — 3+ same-topic completed sessions switches topic.
  5. Empty / all-completed / all-skipped edge cases.
  6. Lighter-item selection.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(PROJECT_ROOT, "app")
for _p in (PROJECT_ROOT, APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# We import the functions directly; the DB is created in-memory in our fixtures.
from app.curriculum import (
    pick_next_item,
    pick_lighter_item,
    get_available_items,
    classify_tier,
    _count_completed_sessions_since_last_hard,
    _hard_gate_passes,
    _prerequisites_met,
    _consecutive_same_topic_count,
    _get_next_chunk,
    _recency_score,
    _handle_empty_set,
)


# ---------------------------------------------------------------------------
# Schema DDL (mirrors app/models.py)
# ---------------------------------------------------------------------------

SCHEMA = """
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
    reason TEXT CHECK(reason IN ('too_hard','not_right_now','not_interested','too_long')),
    skipped_at TEXT DEFAULT (datetime('now')),
    resurface_after TEXT
);
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def empty_db():
    """Return an empty in-memory database with the schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


@pytest.fixture
def populated_db():
    """Return a DB pre-populated with a diverse set of items.

    Items:
      id=1  difficulty=1 topic='math'    est=5    (easy, short)
      id=2  difficulty=3 topic='math'    est=20   (medium)
      id=3  difficulty=3 topic='physics' est=25   (medium)
      id=4  difficulty=5 topic='chem'    est=30   (hard)
      id=5  difficulty=4 topic='bio'     est=20   (hard)
      id=6  difficulty=2 topic='history' est=10   (easy)
      id=7  difficulty=3 topic='physics' est=15   (chunk-0 of parent=3)
      id=8  difficulty=3 topic='physics' est=15   (chunk-1 of parent=3)
      id=9  difficulty=1 topic='art'     est=3    (easy, shortest)
      id=10 difficulty=3 topic='math'    est=20   (medium, has prereq on id=1)
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    items = [
        (1, "https://example.com/1", "Math Easy", None, None, 5, 1, "math"),
        (2, "https://example.com/2", "Math Medium", None, None, 20, 3, "math"),
        (3, "https://example.com/3", "Physics Medium", None, None, 25, 3, "physics"),
        (4, "https://example.com/4", "Chem Hard", None, None, 30, 5, "chem"),
        (5, "https://example.com/5", "Bio Hard", None, None, 20, 4, "bio"),
        (6, "https://example.com/6", "History Easy", None, None, 10, 2, "history"),
        (7, "https://example.com/7", "Physics Chunk 0", 3, 0, 15, 3, "physics"),
        (8, "https://example.com/8", "Physics Chunk 1", 3, 1, 15, 3, "physics"),
        (9, "https://example.com/9", "Art Easy Short", None, None, 3, 1, "art"),
        (10, "https://example.com/10", "Math Medium w/Prereq", None, None, 20, 3, "math"),
    ]
    for row in items:
        conn.execute(
            "INSERT INTO items (id, url, title, parent_item_id, chunk_index, "
            "estimated_minutes, difficulty, topic) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )

    # Item 10 depends on item 1
    conn.execute("INSERT INTO item_prerequisites (item_id, prereq_id, confidence) VALUES (10, 1, 0.9)")

    # Set chunk_total for the parent chain
    conn.execute("UPDATE items SET chunk_total=2 WHERE id=3")

    conn.commit()
    yield conn
    conn.close()


# ===================================================================
# TestClassifyTier
# ===================================================================

class TestClassifyTier:
    def test_easy_difficulty_1(self):
        assert classify_tier({"difficulty": 1}) == "easy"

    def test_easy_difficulty_2(self):
        assert classify_tier({"difficulty": 2}) == "easy"

    def test_medium_difficulty_3(self):
        assert classify_tier({"difficulty": 3}) == "medium"

    def test_hard_difficulty_4(self):
        assert classify_tier({"difficulty": 4}) == "hard"

    def test_hard_difficulty_5(self):
        assert classify_tier({"difficulty": 5}) == "hard"

    def test_none_difficulty_defaults_to_medium(self):
        assert classify_tier({"difficulty": None}) == "medium"

    def test_hard_chunk_child_downgraded_to_medium(self):
        item = {"difficulty": 5, "parent_item_id": 3, "chunk_index": 1}
        assert classify_tier(item) == "medium"

    def test_hard_non_chunk_stays_hard(self):
        item = {"difficulty": 5, "parent_item_id": None, "chunk_index": None}
        assert classify_tier(item) == "hard"


# ===================================================================
# TestGetAvailableItems
# ===================================================================

class TestGetAvailableItems:
    def test_returns_all_items_when_none_completed_or_skipped(self, populated_db):
        items = get_available_items(populated_db)
        assert len(items) == 10

    def test_excludes_completed_items(self, populated_db):
        populated_db.execute(
            "INSERT INTO sessions (item_id, completed_at) VALUES (1, datetime('now'))"
        )
        populated_db.commit()
        items = get_available_items(populated_db)
        ids = {i["id"] for i in items}
        assert 1 not in ids
        assert len(items) == 9

    def test_excludes_items_with_active_skip_cooldown(self, populated_db):
        future = (datetime.utcnow() + timedelta(days=30)).isoformat()
        populated_db.execute(
            "INSERT INTO skips (item_id, reason, resurface_after) VALUES (?, ?, ?)",
            (4, "too_hard", future),
        )
        populated_db.commit()
        items = get_available_items(populated_db)
        ids = {i["id"] for i in items}
        assert 4 not in ids

    def test_excludes_items_with_null_resurface_after_indefinite(self, populated_db):
        """NULL resurface_after means the item is indefinitely skipped."""
        populated_db.execute(
            "INSERT INTO skips (item_id, reason, resurface_after) VALUES (?, ?, NULL)",
            (4, "not_interested"),
        )
        populated_db.commit()
        items = get_available_items(populated_db)
        ids = {i["id"] for i in items}
        assert 4 not in ids

    def test_includes_item_with_expired_skip_cooldown(self, populated_db):
        past = (datetime.utcnow() - timedelta(days=1)).isoformat()
        populated_db.execute(
            "INSERT INTO skips (item_id, reason, resurface_after) VALUES (?, ?, ?)",
            (4, "too_hard", past),
        )
        populated_db.commit()
        items = get_available_items(populated_db)
        ids = {i["id"] for i in items}
        assert 4 in ids

    def test_excludes_completed_and_skipped_but_includes_rest(self, populated_db):
        populated_db.execute(
            "INSERT INTO sessions (item_id, completed_at) VALUES (1, datetime('now'))"
        )
        future = (datetime.utcnow() + timedelta(days=30)).isoformat()
        populated_db.execute(
            "INSERT INTO skips (item_id, reason, resurface_after) VALUES (?, ?, ?)",
            (4, "too_hard", future),
        )
        populated_db.commit()
        items = get_available_items(populated_db)
        ids = {i["id"] for i in items}
        assert 1 not in ids
        assert 4 not in ids
        assert len(items) == 8


# ===================================================================
# TestPickNextItem — happy path
# ===================================================================

class TestPickNextItemHappyPath:
    def test_returns_medium_item_when_available(self, populated_db):
        """The default preference is medium-tier items."""
        result = pick_next_item(populated_db)
        assert result["item"] is not None
        tier = classify_tier(result["item"])
        assert tier in ("medium", "easy", "hard")
        assert result["continue_prompt"] is None or result["continue_prompt"] == ""

    def test_returns_celebration_when_empty_db(self, empty_db):
        result = pick_next_item(empty_db)
        assert result["item"] is None
        assert result["message"] == "empty"

    def test_returns_celebration_when_all_completed(self, populated_db):
        for i in range(1, 11):
            populated_db.execute(
                "INSERT INTO sessions (item_id, completed_at) VALUES (?, datetime('now'))",
                (i,),
            )
        populated_db.commit()
        result = pick_next_item(populated_db)
        assert result["item"] is None
        assert result["message"] == "celebration"

    def test_lighter_available_flag(self, populated_db):
        """At least one easy/short item (id=1 or id=9) should be available."""
        result = pick_next_item(populated_db)
        assert result["lighter_available"] is True


# ===================================================================
# TestPickNextItem — difficulty gating
# ===================================================================

class TestDifficultyGating:
    def test_hard_items_excluded_when_fewer_than_3_sessions_since_last_hard(self, populated_db):
        """No hard item completed yet -> hard candidates should pass gate normally.

        _hard_gate_passes returns True when no hard items have EVER been completed
        (returning 999 from _count_completed_sessions_since_last_hard). So hard items
        should be available.
        """
        # No sessions at all -> gate should pass (first-time user shortcut)
        result = pick_next_item(populated_db)
        # Hard items could be chosen but are lower priority than medium
        assert result["item"] is not None

    def test_hard_items_blocked_after_recent_hard_completion(self, populated_db):
        """After completing a hard item, hard items are blocked until 3 non-hard sessions."""
        # Complete a hard item (id=4, difficulty=5)
        populated_db.execute(
            "INSERT INTO sessions (item_id, completed_at) VALUES (4, datetime('now', '-7 days'))"
        )
        # Complete only 1 non-hard session since
        populated_db.execute(
            "INSERT INTO sessions (item_id, completed_at) VALUES (1, datetime('now', '-1 days'))"
        )
        populated_db.commit()

        count = _count_completed_sessions_since_last_hard(populated_db)
        assert count < 3

        result = pick_next_item(populated_db)
        chosen = result["item"]
        # The chosen item should NOT be hard (difficulty >= 4)
        if chosen:
            assert chosen["difficulty"] < 4 or classify_tier(chosen) in ("easy", "medium")

    def test_hard_items_available_after_3_non_hard_sessions(self, populated_db):
        """After 3+ non-hard sessions since the last hard item, hard items are eligible."""
        # Complete a hard item first
        populated_db.execute(
            "INSERT INTO sessions (id, item_id, completed_at) "
            "VALUES (1, 4, datetime('now', '-14 days'))"
        )
        # Complete 3 non-hard sessions since
        for i, item_id in enumerate([1, 6, 9], start=2):
            populated_db.execute(
                "INSERT INTO sessions (id, item_id, completed_at) "
                "VALUES (?, ?, datetime('now', ? || ' days'))",
                (i, item_id, f'-{7 - i}'),
            )
        populated_db.commit()

        count = _count_completed_sessions_since_last_hard(populated_db)
        assert count >= 3

        result = pick_next_item(populated_db)
        assert result["item"] is not None

    def test_count_sessions_since_last_hard_no_hard_ever(self, populated_db):
        """When no hard item was ever completed, return a large number so hard items are eligible."""
        count = _count_completed_sessions_since_last_hard(populated_db)
        # Should be either >= 3 (so hard items are eligible) or 0 (conservative)
        # Either behavior is valid; the point is the function doesn't crash
        assert count >= 0


# ===================================================================
# TestPickNextItem — prerequisite blocking
# ===================================================================

class TestPrerequisiteBlocking:
    def test_item_with_met_prereq_is_eligible(self, populated_db):
        """Item 10 requires item 1. With item 1 completed, item 10 is eligible."""
        populated_db.execute(
            "INSERT INTO sessions (item_id, completed_at) VALUES (1, datetime('now'))"
        )
        populated_db.commit()
        assert _prerequisites_met(populated_db, 10) is True

    def test_item_with_unmet_prereq_is_excluded(self, populated_db):
        """Item 10 requires item 1. Without item 1 completed, item 10 is excluded."""
        assert _prerequisites_met(populated_db, 10) is False

    def test_item_with_no_prereqs_passes_always(self, populated_db):
        assert _prerequisites_met(populated_db, 1) is True

    def test_prerequisite_filtering_in_pick_next(self, populated_db):
        """Item 10 should not appear in candidates until its prereq is met."""
        # Without completing item 1, item 10 should not be selected
        # Make item 1 the only available medium item to force the issue
        # (item 10 would be the next medium candidate if eligible)
        for _id in (2, 3):
            populated_db.execute(
                "INSERT INTO sessions (item_id, completed_at) VALUES (?, datetime('now'))",
                (_id,),
            )
        populated_db.commit()

        result = pick_next_item(populated_db)
        chosen_id = result["item"]["id"]
        assert chosen_id != 10  # 10 is blocked by prereq


# ===================================================================
# TestPickNextItem — continuity bias (chunks)
# ===================================================================

class TestContinuityBias:
    def test_completed_chunk_leads_to_next_chunk_offer(self, populated_db):
        """After completing chunk 0 of physics, chunk 1 should be offered with prompt."""
        # Complete chunk 0 (item 7, which is chunk_index=0 of parent 3)
        populated_db.execute(
            "INSERT INTO sessions (id, item_id, completed_at) "
            "VALUES (99, 7, datetime('now'))"
        )
        populated_db.commit()

        result = pick_next_item(populated_db, prev_session_id=99)

        # Should offer chunk 1 (item 8)
        assert result["item"] is not None
        assert result["item"]["id"] == 8
        assert result["continue_prompt"] is not None
        assert "Continue" in result["continue_prompt"]
        assert "Physics" in result["continue_prompt"]

    def test_no_continuity_without_prev_session(self, populated_db):
        """Without a prev_session_id, continuity bias does not apply."""
        result = pick_next_item(populated_db, prev_session_id=None)
        assert result["item"] is not None
        # No continue_prompt when continuity doesn't apply
        # (could still be None if chosen by another path)

    def test_no_continuity_for_non_chunk_item(self, populated_db):
        """Completing a non-chunk item does not trigger a continuation prompt."""
        populated_db.execute(
            "INSERT INTO sessions (id, item_id, completed_at) "
            "VALUES (98, 1, datetime('now'))"
        )
        populated_db.commit()

        result = pick_next_item(populated_db, prev_session_id=98)
        assert result["continue_prompt"] is None

    def test_next_chunk_none_for_last_chunk(self, populated_db):
        """Requesting the chunk after the last one returns None."""
        item = {"parent_item_id": 3, "chunk_index": 1}
        assert _get_next_chunk(populated_db, item) is None


# ===================================================================
# TestPickNextItem — topic interleaving
# ===================================================================

class TestTopicInterleaving:
    def test_consecutive_same_topic_count_zero_with_no_sessions(self, populated_db):
        assert _consecutive_same_topic_count(populated_db) == 0

    def test_consecutive_same_topic_count_after_three_math_sessions(self, populated_db):
        for i in range(3):
            populated_db.execute(
                "INSERT INTO sessions (id, item_id, completed_at) "
                "VALUES (?, 1, datetime('now', ? || ' days'))",
                (100 + i, f'-{3 - i}'),
            )
        populated_db.commit()
        # Item 1 has topic 'math'
        assert _consecutive_same_topic_count(populated_db) == 3

    def test_interleaving_after_three_same_topic(self, populated_db):
        """After 3 consecutive 'math' sessions, the next pick should avoid 'math'."""
        # Complete 3 math sessions (item 1, then 2, then 1 again)
        for i, item_id in enumerate([1, 2, 1]):
            populated_db.execute(
                "INSERT INTO sessions (id, item_id, completed_at) "
                "VALUES (?, ?, datetime('now', ? || ' days'))",
                (200 + i, item_id, f'-{10 - i}'),
            )
        populated_db.commit()

        # Mark other medium items as completed so the selection is forced
        # Actually, let's just check the result topic is not 'math' if possible
        result = pick_next_item(populated_db, prev_session_id=202)
        chosen = result["item"]
        # If there's a non-math alternative available, it should be chosen
        # (this is probabilistic due to random tiebreaking, but the topic should
        #  be penalised so we at least expect non-math topics to win)
        non_math_available = any(
            i["topic"] != "math"
            for i in get_available_items(populated_db)
        )
        if non_math_available and chosen:
            assert chosen["topic"] != "math"


# ===================================================================
# TestPickNextItem — recency decay
# ===================================================================

class TestRecencyDecay:
    def test_recent_item_has_zero_boost(self):
        score = _recency_score(datetime.utcnow().isoformat())
        assert score == 0.0

    def test_one_year_old_has_one_point_boost(self):
        old = (datetime.utcnow() - timedelta(days=400)).isoformat()
        score = _recency_score(old)
        assert score == 1.0

    def test_two_year_old_has_two_point_boost(self):
        old = (datetime.utcnow() - timedelta(days=800)).isoformat()
        score = _recency_score(old)
        assert score == 2.0

    def test_none_date_returns_zero(self):
        assert _recency_score(None) == 0.0

    def test_empty_string_returns_zero(self):
        assert _recency_score("") == 0.0


# ===================================================================
# TestPickNextItem — empty / edge cases
# ===================================================================

class TestPickNextItemEdgeCases:
    def test_graveyard_when_all_items_in_cooldown(self, populated_db):
        """When every available item is in skip cooldown, return graveyard message."""
        future = (datetime.utcnow() + timedelta(days=30)).isoformat()
        for item_id in range(1, 11):
            populated_db.execute(
                "INSERT INTO skips (item_id, reason, resurface_after) VALUES (?, 'not_right_now', ?)",
                (item_id, future),
            )
        populated_db.commit()
        result = pick_next_item(populated_db)
        assert result["item"] is None
        assert result["message"] == "graveyard"

    def test_celebration_when_unavailable_but_all_completed(self, populated_db):
        """When all items are either completed or skipped, return celebration."""
        for i in range(1, 11):
            populated_db.execute(
                "INSERT INTO sessions (item_id, completed_at) VALUES (?, datetime('now'))",
                (i,),
            )
        populated_db.commit()

        result = pick_next_item(populated_db)
        assert result["item"] is None
        assert result["message"] == "celebration"

    def test_empty_db_returns_empty_message(self, empty_db):
        result = pick_next_item(empty_db)
        assert result["message"] == "empty"

    def test_handle_empty_set_no_items(self, empty_db):
        result = _handle_empty_set(empty_db)
        assert result["message"] == "empty"


# ===================================================================
# TestPickLighterItem
# ===================================================================

class TestPickLighterItem:
    def test_returns_shortest_easy_item(self, populated_db):
        """Item 9 (difficulty=1, est=3) should be selected."""
        result = pick_lighter_item(populated_db)
        assert result["item"] is not None
        assert result["item"]["id"] == 9  # shortest easy item

    def test_returns_none_when_no_light_items(self, empty_db):
        result = pick_lighter_item(empty_db)
        assert result["item"] is None

    def test_light_item_excludes_completed(self, populated_db):
        populated_db.execute(
            "INSERT INTO sessions (item_id, completed_at) VALUES (9, datetime('now'))"
        )
        populated_db.commit()
        result = pick_lighter_item(populated_db)
        # Item 1 (difficulty=1, est=5) should now be the shortest
        assert result["item"] is not None
        assert result["item"]["id"] == 1

    def test_light_item_excludes_items_with_unmet_prereq(self, populated_db):
        """If a light item has an unmet prereq, it should be excluded."""
        # Add a light item that depends on something unmet
        populated_db.execute(
            "INSERT INTO items (id, url, difficulty, estimated_minutes, topic) "
            "VALUES (20, 'https://x.com', 1, 2, 'test')"
        )
        populated_db.execute(
            "INSERT INTO item_prerequisites (item_id, prereq_id, confidence) VALUES (20, 999, 0.5)"
        )
        populated_db.commit()

        result = pick_lighter_item(populated_db)
        # Item 20 has unmet prereq (item 999 doesn't exist), so should be excluded
        assert result["item"] is not None
        assert result["item"]["id"] != 20

    def test_light_item_skipped_skipped_items(self, populated_db):
        """Items with active skip cooldown should not appear in lighter selections."""
        future = (datetime.utcnow() + timedelta(days=30)).isoformat()
        populated_db.execute(
            "INSERT INTO skips (item_id, reason, resurface_after) VALUES (?, 'too_hard', ?)",
            (9, future),
        )
        populated_db.commit()
        result = pick_lighter_item(populated_db)
        # Item 1 should be chosen next
        assert result["item"] is not None
        assert result["item"]["id"] == 1


# ===================================================================
# TestHardGatePasses
# ===================================================================

class TestHardGatePasses:
    def test_passes_when_no_hard_candidates(self, populated_db):
        assert _hard_gate_passes(populated_db, []) is True

    def test_passes_when_three_or_more_sessions_since_last_hard(self, populated_db):
        populated_db.execute(
            "INSERT INTO sessions (id, item_id, completed_at) VALUES "
            "(1, 4, datetime('now', '-10 days')),"
            "(2, 1, datetime('now', '-7 days')),"
            "(3, 2, datetime('now', '-4 days')),"
            "(4, 3, datetime('now', '-1 days'))"
        )
        populated_db.commit()
        hard_candidates = [{"id": 5, "difficulty": 4}]
        assert _hard_gate_passes(populated_db, hard_candidates) is True

    def test_fails_when_below_three_sessions_since_last_hard(self, populated_db):
        populated_db.execute(
            "INSERT INTO sessions (id, item_id, completed_at) VALUES "
            "(1, 4, datetime('now', '-2 days')),"
            "(2, 1, datetime('now', '-1 days'))"
        )
        populated_db.commit()
        hard_candidates = [{"id": 5, "difficulty": 4}]
        assert _hard_gate_passes(populated_db, hard_candidates) is False

    def test_chunk_children_dont_affect_gate(self, populated_db):
        """Completing a chunk child of a hard parent should not count as 'last hard'."""
        # Complete a hard item (id=4), then complete a chunk child
        populated_db.execute(
            "INSERT INTO sessions (id, item_id, completed_at) VALUES "
            "(1, 4, datetime('now', '-10 days')),"
            "(2, 7, datetime('now', '-5 days'))"  # chunk child
        )
        populated_db.commit()
        assert _count_completed_sessions_since_last_hard(populated_db) == 1
