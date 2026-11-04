"""Lecture content generation — two-phase pipeline.

Phase 1 — Pre-generation (run during classification)
    ``generate_lecture_content(item)`` produces a warmup paragraph and three
    guided questions using the LLM.  The caller stores the result in the
    ``items`` table (``warmup`` / ``guided_questions`` columns).

Phase 2 — Daily personalisation (run at lecture load)
    ``personalize(warmup, recent_topics, current_topic)`` adds a single
    connecting sentence that bridges the current topic to what the user
    recently studied.

Assembly
    ``build_lecture_response(item, db)`` reads the pre-generated content,
    personalises it, creates a session record, and returns the full JSON
    payload expected by the front-end API contract.
"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

from config import get_llm_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM helper
# ---------------------------------------------------------------------------

_WARMUP_FALLBACK_TEMPLATE = "Today's session: {title}. Read/watch and note what you learn."


def _call_llm(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 1024,
) -> str:
    """Call the configured LLM (Anthropic Messages API).

    Raises ``RuntimeError`` on any failure (network, auth, parse).
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

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return result["content"][0]["text"]
    except Exception as exc:
        raise RuntimeError(f"LLM call failed: {exc}") from exc


# We import urllib lazily so that it doesn't shadow the module-level name.
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

# ---------------------------------------------------------------------------
# Phase 1 — Pre-generation
# ---------------------------------------------------------------------------

_GENERATION_PROMPT_TEMPLATE = """\
You are preparing a lecture for a personal learning curriculum. Given the item \
metadata below, generate:
1. A warmup paragraph (2-3 sentences) that explains why this topic matters and \
what to look for. Frame as context, not summary — use phrases like "Based on \
the abstract..." or "This appears to address..."
2. Three guided questions that help the reader engage actively. Questions should \
be observational ("What dataset...?", "How does X relate to Y?") not inferential \
("Why is this correct?").

Metadata: title={title}, description={description}, domain={domain}, \
topic={topic}, difficulty={difficulty}/5

Return ONLY this JSON:
{{
  "warmup": "<2-3 sentences, framed as context not summary>",
  "guided_questions": ["<observational question 1>", "<observational question 2>", \
"<observational question 3>"]
}}"""


def generate_lecture_content(item: dict) -> dict:
    """Generate warmup and guided questions for *item* using the LLM.

    Returns
    -------
    dict
        ``{"warmup": str, "guided_questions": list[str]}``

    When the LLM is unavailable the warmup falls back to a template and the
    guided-questions list is empty — the pipeline never crashes.
    """
    title = item.get("title") or item.get("url", "Untitled")
    description = item.get("description") or ""
    domain = item.get("domain") or ""
    topic = item.get("topic") or ""
    difficulty = item.get("difficulty") or 3

    prompt = _GENERATION_PROMPT_TEMPLATE.format(
        title=title,
        description=description,
        domain=domain,
        topic=topic,
        difficulty=difficulty,
    )

    try:
        raw = _call_llm(prompt, max_tokens=1024)
        parsed = json.loads(raw)

        warmup = str(parsed.get("warmup", ""))
        questions = parsed.get("guided_questions", [])

        # Validate guided_questions is actually a list of strings.
        if not isinstance(questions, list):
            questions = []
        else:
            questions = [str(q) for q in questions if q]

        if warmup:
            return {"warmup": warmup, "guided_questions": questions}

        logger.warning("LLM returned empty warmup; using template fallback.")
    except (RuntimeError, json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.warning("Lecture generation failed (%s); using template fallback.", exc)

    return {
        "warmup": _WARMUP_FALLBACK_TEMPLATE.format(title=title),
        "guided_questions": [],
    }


# ---------------------------------------------------------------------------
# Phase 2 — Daily personalisation
# ---------------------------------------------------------------------------

_PERSONALIZE_PROMPT_TEMPLATE = """\
Given the user's recent learning topics: {recent_topics}
And their current topic: {current_topic}

Add exactly one sentence connecting the current topic to something they've \
recently studied. If there's no meaningful connection, return an empty string.

Return ONLY a JSON object: {{"connection": "<sentence or empty string>"}}"""


def personalize(
    warmup: str,
    recent_topics: Optional[list[str]] = None,
    current_topic: Optional[str] = None,
) -> str:
    """Add a single connecting sentence to *warmup*.

    When the LLM is unavailable, or when there is no meaningful connection
    between topics, an empty string is returned so the caller can use the
    warmup as-is.
    """
    if not recent_topics or not current_topic:
        return ""

    recent = ", ".join(rt for rt in recent_topics if rt)

    prompt = _PERSONALIZE_PROMPT_TEMPLATE.format(
        recent_topics=recent or "(none)",
        current_topic=current_topic,
    )

    try:
        raw = _call_llm(prompt, max_tokens=256)
        parsed = json.loads(raw)
        connection = str(parsed.get("connection", "")).strip()
        return connection
    except (RuntimeError, json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.debug("Personalization skipped (%s).", exc)
        return ""


# ---------------------------------------------------------------------------
# Response assembly
# ---------------------------------------------------------------------------

def build_lecture_response(item: dict, db) -> dict:
    """Assemble the full lecture JSON payload for the front-end API.

    The function:
    1. Reads pre-generated warmup / guided questions from *item* (falling back
       to the template if absent).
    2. Attempts personalisation against recent session topics.
    3. Creates a ``sessions`` record (so the lecture is tracked).
    4. Computes progress statistics.

    Returns
    -------
    dict
        JSON-serialisable dictionary matching the lecture API contract.
    """
    # --- Content ------------------------------------------------------------
    title = item.get("title") or item.get("url", "Untitled")
    warmup = item.get("warmup") or _WARMUP_FALLBACK_TEMPLATE.format(title=title)

    guided_raw = item.get("guided_questions")
    if guided_raw:
        if isinstance(guided_raw, str):
            try:
                guided_questions = json.loads(guided_raw)
            except (json.JSONDecodeError, ValueError):
                guided_questions = [guided_raw]
        elif isinstance(guided_raw, list):
            guided_questions = [str(q) for q in guided_raw]
        else:
            guided_questions = []
    else:
        guided_questions = []

    # --- Personalisation ----------------------------------------------------
    recent_topics = _get_recent_topics(db, limit=3)
    current_topic = item.get("topic")
    connection = personalize(warmup, recent_topics, current_topic)
    if connection:
        warmup = f"{warmup}\n\n{connection}"

    # --- Content metadata ---------------------------------------------------
    content_type = item.get("content_type") or "article"
    url = item.get("url") or ""
    local_file = item.get("local_file_path")

    content_block: dict[str, Any] = {"type": content_type}
    if url:
        content_block["url"] = url
    if local_file:
        content_block["local_file"] = local_file

    # --- Exit ticket --------------------------------------------------------
    exit_ticket_prompt = (
        "What was your biggest takeaway from this session? "
        "What questions do you still have?"
    )

    # --- Estimated minutes --------------------------------------------------
    estimated_minutes = item.get("estimated_minutes") or 15

    # --- Progress -----------------------------------------------------------
    progress = _compute_progress(db)

    return {
        "item_id": item["id"],
        "lecture_title": title,
        "warmup": warmup,
        "content": content_block,
        "guided_questions": guided_questions,
        "exit_ticket_prompt": exit_ticket_prompt,
        "estimated_minutes": estimated_minutes,
        "progress": progress,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_recent_topics(db, limit: int = 3) -> list[str]:
    """Fetch the topics of the most recent *limit* completed sessions."""
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


def _compute_progress(db) -> dict:
    """Return ``{completed, total, percent}`` for the user's curriculum."""
    cursor = db.execute("SELECT COUNT(*) FROM items")
    total = cursor.fetchone()[0]

    cursor = db.execute(
        "SELECT COUNT(DISTINCT item_id) FROM sessions WHERE completed_at IS NOT NULL"
    )
    completed = cursor.fetchone()[0]

    percent = round((completed / total * 100) if total > 0 else 0, 1)

    return {
        "completed": completed,
        "total": total,
        "percent": percent,
    }
