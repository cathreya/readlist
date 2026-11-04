"""Write reading records into Obsidian through its built-in URI handler.

This deliberately does **not** parse or write the vault. Obsidian's
``obsidian://new`` action creates notes and can append to them; ``silent=true``
keeps the note from stealing focus. The only local state is a small registry of
which source already has a note, so frontmatter is written once.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from . import store
from .config import get_note_context, get_obsidian_config
from .taxonomy import normalize_tags

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


class ObsidianError(RuntimeError):
    """Base error; carries the HTTP status the API should return."""

    status = 400


class ObsidianConfigError(ObsidianError):
    status = 400


class ObsidianSendError(ObsidianError):
    status = 502


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_note(
    payload: dict[str, Any],
    card: Optional[dict[str, Any]] = None,
    annotation: str = "",
    extra_tags: Any = None,
    category: str = "",
) -> dict[str, Any]:
    """Create or append one reading note and return its location.

    A note is a *record*, not a flashcard: it contains the source, the passage,
    the surrounding context, the reader's annotation, and a blank ``My notes``
    section. Question/answer text is never written here.
    """
    config = get_obsidian_config()
    if not config["enabled"]:
        raise ObsidianConfigError(
            "Obsidian notes are off. Set FLASHCAP_OBSIDIAN_VAULT in .env."
        )

    vault = str(config["vault"])
    _validate_vault(vault)

    # Clicking "note" twice on an already-recorded card should open the note,
    # not append the same passage again.
    if card and card.get("obsidian_url"):
        return {
            "vault": vault,
            "path": card.get("obsidian_path") or "",
            "open_url": card["obsidian_url"],
            "appended": False,
            "already": True,
        }

    key = f"{vault}|{source_key(payload)}"
    existing = store.get_obsidian_note(key)
    first = existing is None
    path = existing["path"] if existing else note_path(payload, str(config["folder"]))
    day = _note_day(card)
    include_day = first or (existing.get("last_day") != day)

    content = build_content(
        payload,
        first=first,
        card=card,
        annotation=annotation,
        extra_tags=extra_tags,
        category=category,
        day=day,
        include_day=include_day,
    )
    uri = new_uri(vault, path, content, append=not first)
    queued = str(config.get("mode") or "uri") == "queue"
    if queued:
        store.queue_obsidian_note(uri, vault, path)
    else:
        run_uri(uri)

    store.upsert_obsidian_note(
        note_key=key,
        vault=vault,
        path=path,
        source_url=_clean(payload.get("url")) or None,
        source_title=_clean(payload.get("page_title")) or None,
        last_day=day,
    )
    if not card:
        store.record_note_tags([category, *normalize_tags(extra_tags)])

    open_url = open_uri(vault, path)
    if card:
        store.mark_card_obsidian(card["id"], path, open_url)

    return {
        "vault": vault,
        "path": path,
        "open_url": open_url,
        "appended": not first,
        "queued": queued,
    }


def status() -> dict[str, Any]:
    """Return configuration plus registered vault names for the dashboard."""
    config = get_obsidian_config()
    vaults = registered_vaults()
    return {
        **config,
        "vaults": [vault["name"] for vault in vaults],
        "registered": bool(vaults),
    }


def registered_vaults() -> list[dict[str, str]]:
    """Read Obsidian's vault registry (not the vaults themselves)."""
    candidates = [
        Path.home() / "Library/Application Support/obsidian/obsidian.json",
        Path.home() / ".config/obsidian/obsidian.json",
    ]
    appdata = os.getenv("APPDATA", "").strip()
    if appdata:
        candidates.append(Path(appdata) / "obsidian/obsidian.json")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        vaults = []
        for vault_id, info in (data.get("vaults") or {}).items():
            path = str(info.get("path") or "")
            name = Path(path).name if path else vault_id
            vaults.append({"id": vault_id, "name": name, "path": path})
        if vaults:
            return vaults
    return []


# ---------------------------------------------------------------------------
# Note content
# ---------------------------------------------------------------------------

def build_content(
    payload: dict[str, Any],
    first: bool,
    card: Optional[dict[str, Any]] = None,
    annotation: str = "",
    extra_tags: Any = None,
    category: str = "",
    day: str = "",
    include_day: bool = True,
) -> str:
    """Build the Markdown for a new note, or the section appended to one."""
    title = _clean(payload.get("page_title")) or "Untitled reading"
    url = _clean(payload.get("url"))
    site = _clean(payload.get("site"))
    author = _clean(payload.get("author"))
    hint = _clip(_clean(payload.get("hint")), 300)
    annotation = _annotation_text(annotation)

    selection = _clip(
        _clean(payload.get("selection") or payload.get("focus")), 2000
    )
    context = (
        _clip(_clean(payload.get("context")), 400)
        if get_note_context()
        else ""
    )
    if context == selection:
        context = ""

    created_iso, _label = _timestamps(card)
    day = day or created_iso[:10]
    category = _clean(category) or _clean((card or {}).get("category"))
    if category in ("other", "inbox"):
        category = ""
    tags = _note_tags(payload, card, extra_tags, category)

    section: list[str] = []
    if include_day:
        section.extend([f"### {day}", ""])
    if selection:
        section.extend([_blockquote(selection), ""])
    if annotation:
        # Plain text, not an Obsidian callout. The quote above is the source;
        # everything after it is the reader's own note.
        section.extend([annotation, ""])
    if context:
        section.extend(["> [!quote]- Context (excerpt)", _blockquote(context), ""])
    if hint:
        section.extend([f"**Why I saved this:** {hint}", ""])

    if not first:
        return "\n".join(section).rstrip() + "\n"

    frontmatter = [
        "---",
        f"title: {_yaml(title)}",
        f"source: {_yaml(url)}",
        f"author: {_yaml(author)}",
        f"site: {_yaml(site)}",
    ]
    if category:
        frontmatter.append(f"category: {_yaml(category)}")
    frontmatter += [
        f"created: {_yaml(created_iso)}",
        "type: literature-note",
        "tags:",
        *[f"  - {tag}" for tag in tags],
        "---",
        "",
        "## Highlights",
        "",
    ]
    return "\n".join(frontmatter + section).rstrip() + "\n"

def note_path(payload: dict[str, Any], folder: str) -> str:
    title = _safe_title(payload.get("page_title"))
    if not title:
        site = _safe_title(payload.get("site"))
        title = f"{site} reading" if site else "Untitled reading"
    folder = _safe_folder(folder)
    return f"{folder}/{title}.md" if folder else f"{title}.md"


def source_key(payload: dict[str, Any]) -> str:
    url = normalize_url(payload.get("url"))
    if url:
        return url
    title = _clean(payload.get("page_title")).lower()
    site = _clean(payload.get("site")).lower()
    return f"{title or 'untitled'}|{site or 'unknown'}"


def normalize_url(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return ""
        host = parts.hostname.lower()
        if host.startswith("www."):
            host = host[4:]
        port = ""
        if parts.port and parts.port not in (80, 443):
            port = f":{parts.port}"
        path = re.sub(r"/+$", "", parts.path) or "/"
        query = [
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in TRACKING_PARAMS
        ]
        query.sort()
        return urlunsplit(
            (parts.scheme, f"{host}{port}", path, urlencode(query, doseq=True), "")
        )
    except (ValueError, TypeError):
        return ""


# ---------------------------------------------------------------------------
# URI construction + handoff
# ---------------------------------------------------------------------------

def new_uri(vault: str, path: str, content: str, append: bool) -> str:
    params: list[tuple[str, str]] = [
        ("vault", vault),
        ("file", path),
        ("content", content),
        ("silent", "true"),
    ]
    if append:
        params.append(("append", "true"))
    return "obsidian://new?" + _query(params)


def open_uri(vault: str, path: str) -> str:
    return "obsidian://open?" + _query([("vault", vault), ("file", path)])


def run_uri(uri: str) -> None:
    """Hand the URI to Obsidian via LaunchServices without stealing focus."""
    if sys.platform == "darwin":
        command = ["open", "-g", uri]
    elif os.name == "nt":
        command = ["cmd", "/c", "start", "", uri]
    else:
        command = ["xdg-open", uri]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ObsidianSendError(
            "Could not find the system URI opener (open / xdg-open)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ObsidianSendError("Obsidian did not respond in time.") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ObsidianSendError(
            "Could not hand the note to Obsidian"
            + (f": {detail}" if detail else ".")
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_vault(vault: str) -> None:
    vaults = registered_vaults()
    if not vaults:
        return
    names = {entry["name"] for entry in vaults} | {entry["id"] for entry in vaults}
    if vault not in names:
        known = ", ".join(sorted(entry["name"] for entry in vaults))
        raise ObsidianConfigError(
            f"Obsidian vault '{vault}' is not registered. Known vaults: {known}."
        )


def _note_tags(
    payload: dict[str, Any],
    card: Optional[dict[str, Any]],
    extra_tags: Any = None,
    category: str = "",
) -> list[str]:
    tags = ["reading", "flashcap"]
    category = _clean(category) or _clean((card or {}).get("category"))
    if category and category not in ("other", "inbox"):
        tags.extend(normalize_tags(category))
    if card:
        tags.extend(normalize_tags(card.get("tags")))
    tags.extend(normalize_tags(extra_tags))
    result: list[str] = []
    for tag in tags:
        if tag and tag not in result:
            result.append(tag)
    return result[:12]


def _moment(card: Optional[dict[str, Any]]) -> datetime:
    moment = datetime.now(timezone.utc)
    if card and card.get("created_at"):
        try:
            moment = datetime.fromisoformat(
                str(card["created_at"]).replace("Z", "+00:00")
            )
        except ValueError:
            pass
    return moment


def _timestamps(card: Optional[dict[str, Any]]) -> tuple[str, str]:
    moment = _moment(card)
    created_iso = (
        moment.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    return created_iso, moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _note_day(card: Optional[dict[str, Any]]) -> str:
    """Return the capture day in the configured note timezone."""
    name = os.getenv("FLASHCAP_TIMEZONE", "Europe/Dublin").strip() or "UTC"
    try:
        tz = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    return _moment(card).astimezone(tz).strftime("%Y-%m-%d")


def _safe_title(value: Any) -> str:
    text = _clean(value)
    text = re.sub(r'[\\/:*?"<>|#^\[\]\x00-\x1f]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if text.lower().endswith(".md"):
        text = text[:-3].rstrip(" .")
    return text[:100].strip()


def _safe_folder(value: Any) -> str:
    parts = []
    for part in str(value or "").strip("/").split("/"):
        part = re.sub(r'[\\:*?"<>|#^\[\]\x00-\x1f]', " ", part).strip(" .")
        if part and part not in (".", ".."):
            parts.append(part[:80])
    return "/".join(parts)


def _query(params: list[tuple[str, str]]) -> str:
    return "&".join(
        f"{quote(str(key), safe='')}={quote(str(value), safe='')}"
        for key, value in params
    )


def _blockquote(text: str) -> str:
    lines = text.splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _yaml(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _annotation_text(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:3000]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    at = cut.rfind(" ")
    if at > limit * 0.6:
        cut = cut[:at]
    return cut.rstrip(".,;:") + "…"
