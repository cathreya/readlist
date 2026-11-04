"""Quiz policy: what to ask next and how a wrong answer moves the hint ladder.

Pure functions only. Session persistence lives in ``store``, question
generation and grading live in ``llm``; this module decides.
"""

from __future__ import annotations

import json
from typing import Any, Optional

MIN_UNIT_CHARS = 200
MAX_UNIT_CHARS = 1400
MAX_UNITS = 40
MAX_ANSWER_CHARS = 2000
RECALL_EVERY = 4
QUIZ_KINDS = ("explain", "apply", "compare", "predict")
FALLBACK_HINT = "Reread the passage and name its main claim in your own words."


def normalize_units(raw: object) -> list[dict[str, str]]:
    """Validate passages reported by the browser.

    Boundary parse: drop anything malformed, keep document order, and cap the
    payload. Passages shorter than ``MIN_UNIT_CHARS`` are not worth a question.
    """
    if not isinstance(raw, list):
        return []

    units: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()[:40]
        text = " ".join(str(item.get("text") or "").split())[:MAX_UNIT_CHARS]
        if not key or key in seen or len(text) < MIN_UNIT_CHARS:
            continue
        units.append(
            {
                "key": key,
                "text": text,
                "heading": " ".join(str(item.get("heading") or "").split())[:300],
            }
        )
        seen.add(key)
        if len(units) >= MAX_UNITS:
            break
    return units


def select_unit(
    units: list[dict[str, str]],
    asked: list[str],
    coverage: dict[str, dict[str, Any]],
    asked_count: int,
) -> Optional[dict[str, str]]:
    """Pick the next passage: never-quizzed first, older material every fourth.

    Fresh passages arrive newest-first, so the question is about the passage
    just read. Recall passages arrive stalest-first. Returns ``None`` when every
    read passage has been asked in this session.
    """
    asked_keys = set(asked or [])
    fresh: list[tuple[int, dict[str, str]]] = []
    seen: list[tuple[int, dict[str, str]]] = []

    for position, unit in enumerate(units):
        key = unit["key"]
        if key in asked_keys:
            continue
        if key in coverage:
            seen.append((position, unit))
        else:
            fresh.append((position, unit))

    fresh.sort(key=lambda pair: pair[0], reverse=True)
    seen.sort(
        key=lambda pair: (
            str(coverage[pair[1]["key"]].get("last_asked_at") or ""),
            pair[0],
        )
    )

    if asked_count > 0 and asked_count % RECALL_EVERY == RECALL_EVERY - 1 and seen:
        return seen[0][1]
    if fresh:
        return fresh[0][1]
    if seen:
        return seen[0][1]
    return None


def build_question(unit: dict[str, str], generated: dict[str, Any]) -> dict[str, Any]:
    """Wrap a generated question with the per-question state machine."""
    return {
        "unit_key": unit["key"],
        "heading": unit.get("heading") or "",
        "kind": generated.get("kind") or "explain",
        "text": generated["question"],
        "expected": generated["expected"],
        "hints": generated["hints"],
        "hint_level": 0,
        "attempts": 0,
        "errors": [],
        "revealed": False,
    }


def apply_grade(
    question: dict[str, Any], grade: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fold a grade into the question and say what to show the reader.

    A satisfactory grade resolves the question; the caller clears it from the
    session. An unsatisfactory grade reveals the next hint, or the expected
    answer once the hints run out, and keeps the question open.
    """
    updated = json.loads(json.dumps(question))
    updated["attempts"] = int(updated.get("attempts") or 0) + 1
    feedback = str(grade.get("feedback") or "").strip()[:600]
    revealed = bool(updated.get("revealed"))

    reply: dict[str, Any] = {
        "verdict": "not_yet",
        "feedback": feedback,
        "hint": None,
        "revealed": revealed,
    }

    if bool(grade.get("satisfactory")):
        reply["verdict"] = "satisfactory"
        return updated, reply

    if feedback:
        updated.setdefault("errors", []).append(feedback)

    hints = updated.get("hints") or []
    level = int(updated.get("hint_level") or 0)
    if level < len(hints):
        reply["hint"] = {"level": level + 1, "text": hints[level]}
        updated["hint_level"] = level + 1
    else:
        updated["revealed"] = True
        reply["revealed"] = True
        reply["answer"] = [str(point) for point in (updated.get("expected") or [])]

    return updated, reply


def question_view(question: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The client-safe view. Expected points and unoffered hints stay server-side."""
    if not question:
        return None
    return {
        "text": question.get("text") or "",
        "kind": question.get("kind") or "explain",
        "heading": question.get("heading") or "",
        "attempts": int(question.get("attempts") or 0),
        "hint_level": int(question.get("hint_level") or 0),
        "revealed": bool(question.get("revealed")),
    }


def session_view(
    session: dict[str, Any], coverage: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Progress for the panel header. The open question does not count as remaining."""
    units = session.get("units") or []
    asked = set(session.get("asked") or [])
    open_key = (session.get("question") or {}).get("unit_key")
    remaining = asked | ({open_key} if open_key else set())
    fresh = sum(
        1 for unit in units if unit["key"] not in remaining and unit["key"] not in coverage
    )
    seen = sum(
        1 for unit in units if unit["key"] not in remaining and unit["key"] in coverage
    )
    return {
        "id": session.get("id"),
        "source_title": session.get("source_title") or "",
        "asked_count": int(session.get("asked_count") or 0),
        "satisfied_count": int(session.get("satisfied_count") or 0),
        "read_units": len(units),
        "total_units": int(session.get("total_units") or len(units)),
        "fresh_remaining": fresh,
        "seen_remaining": seen,
        "finished": not session.get("question") and fresh == 0 and seen == 0,
    }
