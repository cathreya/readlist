"""Import a Telegram HTML export into the reading library.

Usage::

    python -m capture.backfill
    python -m capture.backfill --html /path/to/messages.html --db /tmp/cards.db

This is the history path. Live messages arrive through ``capture.telegram``.
The import is idempotent: a link already in the library keeps its status.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from . import library, store
from .config import PROJECT_ROOT, get_db_path

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_TRAILING = ".,;:!?\"')"


def parse_export(html_path: str) -> list[dict]:
    """Turn one Telegram export into library rows, oldest message first."""
    with open(html_path, encoding="utf-8") as handle:
        soup = BeautifulSoup(handle.read(), "html.parser")

    items: list[dict] = []
    seen: set[str] = set()

    for message in soup.select("div.message.default.clearfix"):
        message_id = _message_id(message.get("id", ""))
        saved_at = _saved_at(message)
        body = message.find("div", class_="body")
        text_div = body.find("div", class_="text") if body else None
        text = text_div.get_text(" ", strip=True) if text_div else ""

        links: dict[str, dict] = {}
        for anchor in text_div.find_all("a", href=True) if text_div else []:
            href = _clean_url(anchor.get("href", ""))
            key = library.canonical_key(href)
            if key:
                label = anchor.get_text(strip=True)
                links.setdefault(
                    key,
                    {
                        "url": key,
                        "url_key": key,
                        "title": label if label and not label.startswith("http") else None,
                    },
                )

        for raw_url in _URL_RE.findall(text):
            key = library.canonical_key(_clean_url(raw_url))
            if key:
                links.setdefault(
                    key, {"url": key, "url_key": key, "title": None}
                )

        for link in links.values():
            if link["url_key"] in seen:
                continue
            seen.add(link["url_key"])
            items.append(
                {
                    "url": link["url"],
                    "url_key": link["url_key"],
                    "title": link["title"],
                    "site": library.host_of(link["url"]),
                    "description": _description(text),
                    "source": library.SOURCE_EXPORT,
                    "message_id": message_id,
                    "saved_at": saved_at,
                }
            )

        if not links and text.strip():
            key = f"export:{message_id}"
            if key not in seen:
                seen.add(key)
                items.append(
                    {
                        "url": None,
                        "url_key": key,
                        "title": None,
                        "site": None,
                        "description": text.strip(),
                        "source": library.SOURCE_EXPORT,
                        "message_id": message_id,
                        "saved_at": saved_at,
                    }
                )

    return items


def import_export(html_path: str) -> dict[str, int]:
    """Write the export's rows into the library. Safe to run again."""
    items = parse_export(html_path)
    before = store.reading_counts()["total"]
    for item in items:
        store.upsert_reading_item(item)
    after = store.reading_counts()["total"]
    return {"seen": len(items), "added": after - before, "total": after}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Load a Telegram HTML export into the reading library."
    )
    parser.add_argument(
        "--html",
        default=str(PROJECT_ROOT / "messages.html"),
        help="Path to the exported messages.html.",
    )
    parser.add_argument(
        "--db",
        default=get_db_path(),
        help="Path to the Flashcap database.",
    )
    args = parser.parse_args(argv)

    from .server import create_app

    app = create_app(args.db)
    with app.app_context():
        counts = import_export(args.html)

    print(
        f"Read {counts['seen']} rows, added {counts['added']}, "
        f"library now holds {counts['total']}."
    )


def _message_id(raw: object) -> int:
    digits = re.sub(r"\D", "", str(raw or ""))
    return int(digits) if digits else 0


def _saved_at(message) -> str:
    date = message.find("div", class_="date")
    raw = str(date.get("title") if date else "").strip()
    if raw:
        try:
            moment = datetime.strptime(
                raw.replace("UTC", "").strip(), "%d.%m.%Y %H:%M:%S %z"
            )
            return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_url(raw: object) -> str:
    return str(raw or "").strip().rstrip(_TRAILING)


def _description(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    without_links = _URL_RE.sub("", stripped).strip(" -:;|")
    return stripped if without_links else None


if __name__ == "__main__":
    main()
