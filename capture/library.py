"""Identity helpers for the reading library.

The library lives in Flashcap's own database. Telegram and the HTML export feed
it; nothing here reads another app's tables.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .obsidian import normalize_url

SOURCE_EXPORT = "export"
SOURCE_TELEGRAM = "telegram"
SOURCE_MANUAL = "manual"


def canonical_key(url: object) -> str:
    """Return the identity of a saved link, or "" when it is not a web URL."""
    return normalize_url(url)


def host_of(url: object) -> str:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host
