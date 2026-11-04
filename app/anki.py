"""Anki card generation and persistent queue.

Cards are generated via LLM from an item's metadata + exit note, queued in the
local database, and flushed to a local AnkiConnect_ instance when available.

.. _AnkiConnect: https://github.com/FooSoft/anki-connect
"""

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from config import get_llm_config

logger = logging.getLogger(__name__)

ANKI_CONNECT_URL = "http://localhost:8765"
ANKI_CONNECT_TIMEOUT = 10  # seconds

# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------

def _call_llm(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 2048,
) -> str:
    """Call the configured LLM and return the text of its response.

    Raises ``RuntimeError`` on failure (network, auth, parse).
    """
    config = get_llm_config()
    api_key = config.get("api_key", "")
    base_url = config.get("base_url", "https://api.anthropic.com").rstrip("/")
    model = config.get("model", "claude-haiku-4-5-20251001")

    if not api_key:
        raise RuntimeError("LLM API key is not configured")

    url = f"{base_url}/v1/messages"
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        body["system"] = system_prompt

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result["content"][0]["text"]
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError(f"LLM call failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Card generation
# ---------------------------------------------------------------------------

def generate_cards(
    item: dict,
    session_id: int,  # noqa: ARG001  (reserved for future use / audit trail)
    exit_note: Optional[str] = None,
) -> list[dict[str, str]]:
    """Generate 3-5 factual-recall Anki cards from *item* metadata.

    The returned list contains dicts with ``"front"`` and ``"back"`` keys.
    When the LLM is unavailable or returns unparseable output a single
    fallback card is returned so the pipeline never crashes.
    """
    parts: list[str] = []
    if item.get("title"):
        parts.append(f"Title: {item['title']}")
    if item.get("description"):
        parts.append(f"Description: {item['description']}")
    if item.get("blurb"):
        parts.append(f"Blurb: {item['blurb']}")
    if exit_note:
        parts.append(f"Your note: {exit_note}")

    content = "\n".join(parts) if parts else f"Title: {item.get('url', 'Unknown item')}"

    prompt = (
        "Generate 3-5 Anki flashcards (front/back pairs) based on this content. "
        "Focus on key concepts worth remembering.\n\n"
        f"Content:\n{content}\n\n"
        'Return ONLY a JSON array of objects with "front" and "back" keys, e.g.\n'
        '[{"front": "What is X?", "back": "X is ..."}, ...]'
    )

    try:
        raw = _call_llm(prompt, max_tokens=2048)
        cards = json.loads(raw)

        # Validate structure.
        if not isinstance(cards, list):
            raise ValueError("LLM response was not a list")

        validated: list[dict[str, str]] = []
        for card in cards:
            if isinstance(card, dict) and "front" in card and "back" in card:
                validated.append({"front": str(card["front"]), "back": str(card["back"])})

        if validated:
            return validated

        logger.warning("LLM returned empty/unparseable card list; using fallback.")
    except (RuntimeError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Card generation failed (%s); using fallback.", exc)

    # Fallback — produce a single safe card.
    title = item.get("title") or item.get("url", "Unknown")
    desc = item.get("description") or "See the original item."
    return [{"front": f"Key points from: {title}", "back": desc}]


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------

def queue_cards(
    db,
    item_id: int,
    session_id: int,
    cards: list[dict[str, str]],
) -> int:
    """Insert *cards* into the ``card_queue`` table.

    Returns the number of cards inserted.
    """
    count = 0
    for card in cards:
        db.execute(
            """
            INSERT INTO card_queue (item_id, session_id, front, back)
            VALUES (?, ?, ?, ?)
            """,
            (item_id, session_id, card["front"], card["back"]),
        )
        count += 1
    db.commit()
    return count


# ---------------------------------------------------------------------------
# AnkiConnect integration
# ---------------------------------------------------------------------------

def _anki_request(payload: dict) -> Optional[dict]:
    """Send a JSON-RPC request to AnkiConnect.

    Returns the ``result`` field on success, or ``None`` on connectivity errors.
    """
    try:
        req = urllib.request.Request(
            ANKI_CONNECT_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=ANKI_CONNECT_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("error"):
            logger.warning("AnkiConnect error: %s", body["error"])
            return None
        return body.get("result")
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as exc:
        logger.debug("AnkiConnect unreachable: %s", exc)
        return None


def flush_card_queue(db) -> tuple[int, int]:
    """Flush all un-flushed queued cards to AnkiConnect.

    Returns
    -------
    tuple[int, int]
        ``(flushed_count, remaining_count)`` — cards that were successfully sent
        vs cards still in the queue (either skipped due to error or AnkiConnect
        being offline).
    """
    cursor = db.execute(
        "SELECT * FROM card_queue WHERE flushed = 0 ORDER BY id ASC"
    )
    pending = [_dict_from_row(r) for r in cursor.fetchall()]

    if not pending:
        return (0, 0)

    # Check connectivity with a lightweight ping first.
    ping = _anki_request({"action": "version", "version": 6, "params": {}})
    if ping is None:
        # AnkiConnect is unreachable — leave everything queued.
        cursor = db.execute(
            "SELECT COUNT(*) FROM card_queue WHERE flushed = 0"
        )
        return (0, cursor.fetchone()[0])

    flushed = 0
    for row in pending:
        payload = {
            "action": "addNote",
            "version": 6,
            "params": {
                "note": {
                    "deckName": "Lecture Today",
                    "modelName": "Basic",
                    "fields": {"Front": row["front"], "Back": row["back"]},
                    "tags": ["lecture-today"],
                }
            },
        }
        result = _anki_request(payload)
        if result is not None:
            db.execute(
                "UPDATE card_queue SET flushed = 1 WHERE id = ?",
                (row["id"],),
            )
            flushed += 1

    db.commit()

    cursor = db.execute(
        "SELECT COUNT(*) FROM card_queue WHERE flushed = 0"
    )
    remaining = cursor.fetchone()[0]
    return (flushed, remaining)


def get_pending_card_count(db) -> int:
    """Return how many cards are still waiting to be flushed."""
    cursor = db.execute("SELECT COUNT(*) FROM card_queue WHERE flushed = 0")
    return cursor.fetchone()[0]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dict_from_row(row) -> dict:
    return dict(row)
