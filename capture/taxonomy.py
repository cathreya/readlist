"""Category and tag vocabulary.

Categories are intentionally broad and low-cardinality; tags carry the specific
concepts. Cards, notes, and the reading list share the same list.
"""

from __future__ import annotations

import re
from typing import Iterable

CARD_CATEGORIES = [
    "machine-learning",
    "algorithms",
    "systems",
    "math",
    "physics",
    "neuroscience",
    "programming",
    "philosophy",
    "history",
    "economics",
    "biology",
    "design",
    "career",
    "ireland",
    "india",
    "other",
]

# ``inbox`` is only used by the no-LLM fallback: it means "captured, not yet
# categorised". It is deliberately not offered to the model.
UI_CATEGORIES = CARD_CATEGORIES + ["inbox"]

_CATEGORY_RE = re.compile(r"[^a-z0-9-]+")
_TAG_RE = re.compile(r"[^a-z0-9+#.\-]+")


def normalize_category(value: object, fallback: str = "other") -> str:
    """Turn arbitrary input into a stable category slug."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    text = _CATEGORY_RE.sub("-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:40] or fallback


def normalize_tags(value: object, limit: int = 8) -> list[str]:
    """Normalise tags to a deduplicated list of lowercase slugs.

    Accepts either a list or a comma/newline/semicolon separated string.
    Spaces inside a tag become hyphens (``"linear algebra"`` →
    ``"linear-algebra"``).
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts: Iterable[object] = re.split(r"[,;\n]", value)
    elif isinstance(value, (list, tuple, set)):
        parts = value
    else:
        parts = [value]

    tags: list[str] = []
    seen: set[str] = set()
    for part in parts:
        tag = str(part or "").strip().lower()
        if not tag:
            continue
        tag = re.sub(r"\s+", "-", tag)
        tag = _TAG_RE.sub("-", tag)
        tag = re.sub(r"-{2,}", "-", tag).strip("-.#")
        if not tag or len(tag) > 40 or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= limit:
            break
    return tags
