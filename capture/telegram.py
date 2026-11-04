"""Telegram long polling for the reading library.

The server sits behind a tailnet and its only public port is taken, so inbound
webhooks are not an option: we pull with ``getUpdates`` on a background thread.
Stdlib only, because the remote deployment installs ``capture/requirements.txt``
and that file does not carry ``requests``.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import library, store
from .config import get_telegram_config

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram_offset"
UPDATE_KINDS = ("message", "edited_message", "channel_post", "edited_channel_post")
_BARE_URL = re.compile(r"https?://[^\s<>\"']+")
_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    """A poll failed for a reason worth logging and retrying."""


def fetch_updates(token: str, offset: int = 0, timeout: int = 25) -> list[dict]:
    params: dict[str, object] = {
        "timeout": timeout,
        "limit": 100,
        "allowed_updates": json.dumps(list(UPDATE_KINDS)),
    }
    if offset:
        params["offset"] = offset
    url = _API.format(token=token, method="getUpdates")
    url = f"{url}?{urllib.parse.urlencode(params)}"

    try:
        with urllib.request.urlopen(url, timeout=timeout + 15) as response:
            payload = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _telegram_error(exc) from exc

    if not payload.get("ok"):
        raise TelegramError(payload.get("description") or "Telegram rejected the poll")
    return payload.get("result") or []


def forward_message(
    token: str, done_chat_id: str, from_chat_id: str, message_id: int
) -> dict:
    return _call(
        token,
        "forwardMessage",
        {
            "chat_id": done_chat_id,
            "from_chat_id": from_chat_id,
            "message_id": message_id,
        },
    )


def delete_message(token: str, chat_id: str, message_id: int) -> None:
    _call(token, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def send_message(token: str, chat_id: str, text: str) -> dict:
    return _call(token, "sendMessage", {"chat_id": chat_id, "text": text})


def undo_done(item: dict) -> dict:
    """Put a finished item back: repost the source, remove the forwarded copy.

    When the delete had failed, the source still exists, so only the forwarded
    copy is removed. A failed repost raises, and the caller keeps the item done.
    """
    config = get_telegram_config()
    token = config["token"]
    done_chat_id = config["done_chat_id"]
    if not token:
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
    if not done_chat_id:
        raise TelegramError("TELEGRAM_DONE_CHAT_ID is not set")

    result = {"ok": True, "repost": False, "unforwarded": False}

    if not item.get("archive_error"):
        text = str(item.get("url") or item.get("description") or "").strip()
        if not text:
            raise TelegramError("Nothing to repost")
        sent = send_message(token, str(item["chat_id"]), text)
        result["message_id"] = int(sent["message_id"])
        result["repost"] = True

    archive_id = item.get("archive_message_id")
    if archive_id:
        try:
            delete_message(token, done_chat_id, int(archive_id))
            result["unforwarded"] = True
        except TelegramError as exc:
            result["error"] = str(exc)
    return result


def archive_done(item: dict) -> dict:
    """Forward a finished item to the done chat, then delete the original.

    Forward first. A failed forward leaves the source intact and the caller can
    keep the item out of ``done``. A failed delete is reported but the item is
    already archived, so a retry does not forward it twice.
    """
    config = get_telegram_config()
    token = config["token"]
    done_chat_id = config["done_chat_id"]

    if not token:
        return {"ok": False, "skipped": "TELEGRAM_BOT_TOKEN is not set"}
    if not done_chat_id:
        return {"ok": False, "skipped": "TELEGRAM_DONE_CHAT_ID is not set"}
    if item.get("archived_at"):
        return {"ok": True, "already": True}

    chat_id = str(item.get("chat_id") or "")
    message_id = item.get("message_id")
    if not chat_id or not message_id:
        return {"ok": False, "skipped": "not a Telegram message"}
    if done_chat_id == chat_id:
        return {"ok": False, "skipped": "source and done chat are the same"}

    forwarded = forward_message(token, done_chat_id, chat_id, int(message_id))
    store.mark_reading_archived(int(item["id"]), int(forwarded["message_id"]))

    try:
        delete_message(token, chat_id, int(message_id))
    except TelegramError as exc:
        store.set_reading_archive_error(int(item["id"]), str(exc))
        return {"ok": True, "forwarded": True, "deleted": False, "error": str(exc)}
    return {"ok": True, "forwarded": True, "deleted": True}


def _call(token: str, method: str, payload: dict, timeout: int = 20) -> dict:
    request = urllib.request.Request(
        _API.format(token=token, method=method),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _telegram_error(exc) from exc

    if not result.get("ok"):
        raise TelegramError(result.get("description") or "Telegram rejected the request")
    return result.get("result") or {}


def _telegram_error(exc: Exception) -> TelegramError:
    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            detail = json.load(exc).get("description", "")
        except (ValueError, OSError):
            pass
        return TelegramError(f"Telegram returned {exc.code}: {detail or exc.reason}")
    return TelegramError(f"Cannot reach Telegram: {exc}")


def parse_update(update: dict) -> list[dict]:
    """Turn one Telegram update into library items.

    A message can carry several links. Each becomes its own row. A text-only
    message becomes a note keyed by its message id.
    """
    message = next((update[key] for key in UPDATE_KINDS if update.get(key)), None)
    if not isinstance(message, dict):
        return []

    text = str(message.get("text") or message.get("caption") or "")
    entities = message.get("entities") or message.get("caption_entities") or []
    preview = message.get("link_preview_options") or message.get("web_page") or {}
    chat_id = str((message.get("chat") or {}).get("id") or "")
    message_id = _int_or_none(message.get("message_id"))
    saved_at = _saved_at(message.get("date"))

    links: list[dict] = []
    seen: set[str] = set()

    for entity in entities:
        if not isinstance(entity, dict):
            continue
        kind = entity.get("type")
        if kind == "text_link" and entity.get("url"):
            raw_url = str(entity["url"])
            title = _entity_text(text, entity)
        elif kind == "url":
            raw_url = _entity_text(text, entity)
            title = ""
        else:
            continue
        key = library.canonical_key(raw_url)
        if not key or key in seen:
            continue
        seen.add(key)
        links.append({"url": key, "url_key": key, "title": title or None, "_raw": raw_url})

    for raw_url in _BARE_URL.findall(text):
        raw_url = raw_url.rstrip(".,;:!?)]}")
        key = library.canonical_key(raw_url)
        if not key or key in seen:
            continue
        seen.add(key)
        links.append({"url": key, "url_key": key, "title": None, "_raw": raw_url})

    items: list[dict] = []
    for link in links:
        preview_url = library.canonical_key(preview.get("url"))
        matches = preview_url == link["url_key"] or len(links) == 1
        title = link["title"] or (preview.get("title") if matches else None)
        site = (
            (preview.get("site_name") if matches else None)
            or library.host_of(link["url"])
            or None
        )
        items.append(
            {
                "url": link["url"],
                "url_key": link["url_key"],
                "title": title,
                "site": site,
                "description": _description(text, link["_raw"], link["url"], preview),
                "source": library.SOURCE_TELEGRAM,
                "chat_id": chat_id,
                "message_id": message_id,
                "saved_at": saved_at,
            }
        )

    if items:
        return items

    stripped = text.strip()
    if not stripped:
        return []

    return [
        {
            "url": None,
            "url_key": f"tg:{chat_id}:{message_id}",
            "title": None,
            "site": None,
            "description": stripped,
            "source": library.SOURCE_TELEGRAM,
            "chat_id": chat_id,
            "message_id": message_id,
            "saved_at": saved_at,
        }
    ]


def ingest_updates(
    app, updates: list[dict], chat_ids: tuple[str, ...] = ()
) -> dict[str, int]:
    """Write a batch and advance the offset. Safe to replay after a crash."""
    written = 0
    highest = 0

    with app.app_context():
        for update in updates:
            update_id = _int_or_none(update.get("update_id"))
            if update_id:
                highest = max(highest, update_id)
            for item in parse_update(update):
                if chat_ids and item["chat_id"] not in chat_ids:
                    continue
                store.upsert_reading_item(item)
                written += 1
        if highest:
            store.set_state(OFFSET_KEY, str(highest + 1))

    return {"items": written, "offset": highest + 1 if highest else 0}


def run_forever(app) -> None:
    """Poll Telegram until the process stops. Started only from ``main()``."""
    config = get_telegram_config()
    if not config["enabled"]:
        return

    token = config["token"]
    chat_ids = config["chat_ids"]
    timeout = config["timeout"]

    with app.app_context():
        offset = int(store.get_state(OFFSET_KEY, "0") or 0)

    backoff = 5
    while True:
        try:
            updates = fetch_updates(token, offset, timeout)
            backoff = 5
            if updates:
                result = ingest_updates(app, updates, chat_ids)
                if result["offset"]:
                    offset = result["offset"]
        except TelegramError as exc:
            log.warning("Telegram poll failed: %s", exc)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
        except Exception:  # noqa: BLE001 - the loop must survive one bad batch
            log.exception("Telegram ingest failed; retrying")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


def _entity_text(text: str, entity: dict) -> str:
    """Slice an entity by Telegram's UTF-16 offsets."""
    try:
        offset = int(entity.get("offset", 0))
        length = int(entity.get("length", 0))
    except (TypeError, ValueError):
        return ""
    raw = text.encode("utf-16-le")
    start = offset * 2
    end = (offset + length) * 2
    return raw[start:end].decode("utf-16-le", "ignore").strip()


def _description(text: str, raw_url: str, url_key: str, preview: dict) -> str | None:
    stripped = text.strip()
    if stripped and stripped not in {raw_url, url_key}:
        return stripped
    description = preview.get("description")
    return str(description).strip() if description else None


def _saved_at(epoch: object) -> str:
    moment = datetime.now(timezone.utc)
    if isinstance(epoch, (int, str)) and not isinstance(epoch, bool):
        try:
            moment = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
        except (ValueError, OSError):
            pass
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
