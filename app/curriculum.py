"""Curriculum selection algorithm — picks the next item for the user to study.

Priority order (higher wins):
1. Difficulty gating — hard items (difficulty 4-5, non-chunk) require 3+ completed
   sessions since the last hard item. Chunk children of hard parents are treated as medium.
2. Prerequisite block — exclude items with unmet prerequisites from the candidate set.
3. Continuity bias — if the previous session was chunk N, offer chunk N+1 first.
   (Returns a continue_prompt with the offer.)
4. Topic interleaving — after 3 consecutive same-topic completed sessions, pick a
   different topic.  Interleaving beats continuity.
5. Recency decay — items saved >12mo ago get +1 priority boost, >24mo get +2.
6. Random tiebreaker when multiple items are equally eligible.
"""

import random
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from models import get_db, compute_resurface_date

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dict_from_row(row: Optional[sqlite3.Row]) -> Optional[dict]:
    """Convert a ``sqlite3.Row`` to a plain dict, or return ``None``."""
    if row is None:
        return None
    return dict(row)


def _strptime_or_none(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 string to datetime, returning None on failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Candidate-set building helpers
# ---------------------------------------------------------------------------

def get_available_items(db) -> list[dict]:
    """Return every item available for reading.

    An item is available when it:
    * has **no** completed session, **and**
    * does **not** have an active skip cooldown (``resurface_after`` in the future
      or ``NULL`` meaning indefinitely skipped).
    """
    cursor = db.execute(
        """
        SELECT i.* FROM items i
        WHERE i.status NOT IN ('completed', 'dead', 'error', 'in_progress')
          AND i.difficulty IS NOT NULL
          AND i.id NOT IN (
              SELECT item_id FROM sessions WHERE completed_at IS NOT NULL
          )
          AND NOT EXISTS (
              SELECT 1 FROM skips s
              WHERE s.item_id = i.id
                AND (s.resurface_after IS NULL OR s.resurface_after > datetime('now'))
          )
          AND (i.error_reason IS NULL OR i.error_reason != 'graveyard')
        """
    )
    return [_dict_from_row(row) for row in cursor.fetchall()]


def classify_tier(item: dict) -> str:
    """Return ``"easy"``, ``"medium"``, or ``"hard"`` for *item*.

    Chunk children of hard parents are downgraded to ``"medium"``.
    """
    difficulty = item.get("difficulty")
    if difficulty is None:
        return "medium"  # safe default

    is_chunk_child = (
        item.get("parent_item_id") is not None
        and item.get("chunk_index") is not None
    )

    if is_chunk_child and difficulty >= 4:
        return "medium"
    if difficulty >= 4:
        return "hard"
    if difficulty >= 3:
        return "medium"
    return "easy"


def _is_hard(item: dict) -> bool:
    """Is *item* a hard (non-chunk) item?"""
    if item.get("difficulty", 0) < 4:
        return False
    is_chunk_child = (
        item.get("parent_item_id") is not None
        and item.get("chunk_index") is not None
    )
    return not is_chunk_child


# ---------------------------------------------------------------------------
# Difficulty gating
# ---------------------------------------------------------------------------

def _count_completed_sessions_since_last_hard(db) -> int:
    """Number of completed sessions after the most-recently-completed hard item.

    Returns a large number (999) when no hard item has ever been completed so
    that the gate passes trivially for first-time users.
    """
    cursor = db.execute(
        """
        SELECT COUNT(*) FROM sessions
        WHERE completed_at IS NOT NULL
          AND completed_at > COALESCE(
              (SELECT MAX(s.completed_at) FROM sessions s
               JOIN items i ON i.id = s.item_id
               WHERE s.completed_at IS NOT NULL
                 AND i.difficulty >= 4
                 AND i.parent_item_id IS NULL),
              '1970-01-01'
          )
        """
    )
    return cursor.fetchone()[0]


def _hard_gate_passes(db, hard_candidates: list[dict]) -> bool:
    """Return ``True`` when hard items may be offered.

    The gate passes if there are no hard candidates to gate, **or** if at least
    3 sessions have been completed since the last hard item.
    """
    if not hard_candidates:
        return True
    return _count_completed_sessions_since_last_hard(db) >= 3


# ---------------------------------------------------------------------------
# Prerequisite checking
# ---------------------------------------------------------------------------

def _prerequisites_met(db, item_id: int) -> bool:
    """Return ``True`` when every prerequisite of *item_id* has a completed session."""
    cursor = db.execute(
        """
        SELECT COUNT(*) FROM item_prerequisites ip
        WHERE ip.item_id = ?
          AND ip.prereq_id NOT IN (
              SELECT item_id FROM sessions WHERE completed_at IS NOT NULL
          )
        """,
        (item_id,),
    )
    return cursor.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Continuity bias
# ---------------------------------------------------------------------------

def _get_prev_item(db, prev_session_id: Optional[int]) -> Optional[dict]:
    """Fetch the item associated with the previous *completed* session.

    Returns ``None`` when *prev_session_id* is ``None``, the session doesn't
    exist, or the session hasn't been completed yet.
    """
    if prev_session_id is None:
        return None
    cursor = db.execute(
        """
        SELECT i.* FROM items i
        JOIN sessions s ON s.item_id = i.id
        WHERE s.id = ? AND s.completed_at IS NOT NULL
        """,
        (prev_session_id,),
    )
    return _dict_from_row(cursor.fetchone())


def _get_next_chunk(db, prev_item: dict) -> Optional[dict]:
    """Return the chunk immediately following *prev_item*, or ``None``."""
    if prev_item.get("chunk_index") is None or prev_item.get("parent_item_id") is None:
        return None
    cursor = db.execute(
        """
        SELECT * FROM items
        WHERE parent_item_id = ? AND chunk_index = ?
        """,
        (prev_item["parent_item_id"], prev_item["chunk_index"] + 1),
    )
    return _dict_from_row(cursor.fetchone())


# ---------------------------------------------------------------------------
# Topic interleaving
# ---------------------------------------------------------------------------

def _consecutive_same_topic_count(db) -> int:
    """Number of most-recently completed sessions sharing the same topic."""
    cursor = db.execute(
        """
        SELECT i.topic FROM sessions s
        JOIN items i ON i.id = s.item_id
        WHERE s.completed_at IS NOT NULL
        ORDER BY s.completed_at DESC
        """
    )
    rows = cursor.fetchall()
    if not rows or rows[0]["topic"] is None:
        return 0

    target = rows[0]["topic"]
    count = 0
    for row in rows:
        if row["topic"] == target:
            count += 1
        else:
            break
    return count


def _recent_topics(db, limit: int = 3) -> list[str]:
    """Return the topics of the last *limit* completed sessions (most recent first)."""
    cursor = db.execute(
        """
        SELECT i.topic FROM sessions s
        JOIN items i ON i.id = s.item_id
        WHERE s.completed_at IS NOT NULL AND i.topic IS NOT NULL
        ORDER BY s.completed_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [row["topic"] for row in cursor.fetchall() if row["topic"]]


# ---------------------------------------------------------------------------
# Lighter-item check
# ---------------------------------------------------------------------------

def _check_lighter_available(db) -> bool:
    """Is there an eligible easy/short item the user could switch to?"""
    available = get_available_items(db)
    for item in available:
        diff = item.get("difficulty")
        est = item.get("estimated_minutes")
        if diff is not None and diff <= 2 and est is not None and est < 15:
            if _prerequisites_met(db, item["id"]):
                return True
    return False


# ---------------------------------------------------------------------------
# Empty-set handling
# ---------------------------------------------------------------------------

def _handle_empty_set(db) -> dict:
    """Return a descriptive response when no items are available."""
    cursor = db.execute("SELECT COUNT(*) FROM items")
    total = cursor.fetchone()[0]

    if total == 0:
        return {"item": None, "message": "empty"}

    cursor = db.execute(
        "SELECT COUNT(DISTINCT item_id) FROM sessions WHERE completed_at IS NOT NULL"
    )
    completed = cursor.fetchone()[0]

    cursor = db.execute(
        """
        SELECT COUNT(DISTINCT item_id) FROM skips
        WHERE resurface_after IS NULL OR resurface_after > datetime('now')
        """
    )
    cooling_down = cursor.fetchone()[0]

    cursor = db.execute(
        "SELECT COUNT(DISTINCT item_id) FROM sessions WHERE completed_at IS NULL"
    )
    in_progress = cursor.fetchone()[0]

    unavailable = completed + cooling_down + in_progress

    if completed > 0 and unavailable >= total:
        return {
            "item": None,
            "message": "celebration",
        }
    if cooling_down > 0:
        return {
            "item": None,
            "message": "graveyard",
        }
    return {"item": None, "message": "empty"}


def _recency_score(created_at: Any) -> float:
    """Compute recency-decay priority boost (older = higher)."""
    dt = _strptime_or_none(created_at)
    if dt is None:
        return 0.0
    age = datetime.utcnow() - dt
    if age > timedelta(days=365 * 2):
        return 2.0
    if age > timedelta(days=365):
        return 1.0
    return 0.0


# ===================================================================
# Public API
# ===================================================================

def pick_next_item(
    db, prev_session_id: Optional[int] = None
) -> dict:
    """Select the next item for the user to study.

    Parameters
    ----------
    db:
        Open database connection.
    prev_session_id:
        ID of the most recent session (completed or in-progress).  Pass
        ``None`` for a brand-new user (no continuity/interleaving applies).

    Returns
    -------
    dict
        ``{"item": dict | None, "continue_prompt": str | None,
        "lighter_available": bool, "message": str | None}``
    """
    # ---- 1.  Build the candidate set ---------------------------------------
    all_available = get_available_items(db)
    if not all_available:
        return _handle_empty_set(db)

    # Partition by tier.
    easy: list[dict] = []
    medium: list[dict] = []
    hard: list[dict] = []
    for item in all_available:
        tier = classify_tier(item)
        if tier == "easy":
            easy.append(item)
        elif tier == "hard":
            hard.append(item)
        else:
            medium.append(item)

    # ---- 2.  Difficulty gating ----------------------------------------------
    if not _hard_gate_passes(db, hard):
        hard = []

    # ---- 3.  Prerequisite filtering -----------------------------------------
    for tier_list in (easy, medium, hard):
        tier_list[:] = [it for it in tier_list if _prerequisites_met(db, it["id"])]

    # Merge tiers in priority order: medium (sweet-spot) > hard > easy.
    eligible: list[dict] = medium + hard + easy
    if not eligible:
        return _handle_empty_set(db)

    prev_item = _get_prev_item(db, prev_session_id)
    consecutive_topic = _consecutive_same_topic_count(db)

    # ---- 4.  Continuity bias (may lose to interleaving) ----------------------
    if prev_item and prev_item.get("chunk_index") is not None:
        next_chunk = _get_next_chunk(db, prev_item)
        if next_chunk and any(c["id"] == next_chunk["id"] for c in eligible):
            # Interleaving beats continuity: if 3+ same-topic, skip the offer.
            if consecutive_topic < 3 or (
                next_chunk.get("topic") and prev_item.get("topic")
                and next_chunk["topic"] != prev_item["topic"]
            ):
                title = prev_item.get("title") or prev_item.get("url", "item")
                total = prev_item["chunk_total"] or 1
                next_idx = next_chunk["chunk_index"] or 0
                prompt = (
                    f"Continue {title} "
                    f"(Session {next_idx + 1} of {total}) "
                    f"or something different?"
                )
                return {
                    "item": next_chunk,
                    "continue_prompt": prompt,
                    "lighter_available": _check_lighter_available(db),
                }

    # ---- 5.  Topic interleaving + recency decay + tiebreaker ----------------
    topic_blocked: Optional[str] = None
    if consecutive_topic >= 3:
        # Block the hot topic so we steer toward something else.
        recent = _recent_topics(db, limit=1)
        if recent:
            topic_blocked = recent[0]

    scored: list[tuple[float, dict]] = []
    for item in eligible:
        # Skip continuity chunks already handled above.
        if (
            prev_item
            and prev_item.get("chunk_index") is not None
            and _get_next_chunk(db, prev_item)
            and _get_next_chunk(db, prev_item)["id"] == item["id"]
        ):
            continue

        score = _recency_score(item.get("created_at"))

        # Strong penalty for blocked topic.
        if topic_blocked and item.get("topic") == topic_blocked:
            score -= 10.0

        scored.append((score, item))

    if not scored:
        # Everything was blocked by interleaving — lift the ban rather than
        # force an empty selection.
        for item in eligible:
            if (
                prev_item
                and prev_item.get("chunk_index") is not None
                and _get_next_chunk(db, prev_item)
                and _get_next_chunk(db, prev_item)["id"] == item["id"]
            ):
                continue
            scored.append((_recency_score(item.get("created_at")), item))

    if not scored:
        return _handle_empty_set(db)

    max_score = max(s[0] for s in scored)
    top_candidates = [item for score, item in scored if score >= max_score - 0.001]
    chosen = random.choice(top_candidates)

    return {
        "item": chosen,
        "continue_prompt": None,
        "lighter_available": _check_lighter_available(db),
    }


def pick_lighter_item(db) -> dict:
    """Pick the shortest eligible easy item (difficulty 1-2, estimated <15 min).

    Returns
    -------
    dict
        ``{"item": dict | None}``
    """
    all_available = get_available_items(db)
    eligible = [it for it in all_available if _prerequisites_met(db, it["id"])]

    light = [
        it
        for it in eligible
        if it.get("difficulty") is not None
        and it["difficulty"] <= 2
        and it.get("estimated_minutes") is not None
        and it["estimated_minutes"] < 15
    ]

    if not light:
        return {"item": None}

    light.sort(key=lambda x: x["estimated_minutes"])
    return {"item": light[0]}
