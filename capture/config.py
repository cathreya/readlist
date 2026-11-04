"""Configuration for Flashcap.

Deliberately independent of the Lecture Today app so this stays a small,
self-contained tool. It reads the same ``.env`` file at the project root.

Environment variables
---------------------
``LLM_API_KEY`` / ``ANTHROPIC_API_KEY``
    Key for an Anthropic-compatible Messages API.
``LLM_BASE_URL``
    Base URL, e.g. ``https://api.anthropic.com`` or
    ``https://api.deepseek.com/anthropic``.
``LLM_MODEL``
    Model name.
``FLASHCAP_PORT``
    Local port. Defaults to 8765.
``FLASHCAP_DB``
    SQLite file path. Defaults to ``capture/flashcards.db``.
"""

from __future__ import annotations

import os
from pathlib import Path

CAPTURE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CAPTURE_DIR.parent
ENV_PATH = PROJECT_ROOT / ".env"


def _load_dotenv() -> None:
    """Load ``.env`` without requiring python-dotenv."""
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Do not clobber variables that were set explicitly.
        os.environ.setdefault(key, value)


_load_dotenv()


def get_llm_config() -> dict[str, str]:
    """Return the active LLM provider configuration."""
    return {
        "api_key": os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY", ""),
        "base_url": os.getenv("LLM_BASE_URL", "https://api.anthropic.com").rstrip("/"),
        "model": os.getenv("LLM_MODEL", "claude-haiku-4-5-20251001"),
    }


def get_host() -> str:
    """Return the bind address.

    Defaults to loopback. On a remote box, bind the Tailscale IP so the HTTP
    port is reachable only over the tailnet (or use Tailscale Serve and keep
    this on 127.0.0.1).
    """
    return os.getenv("FLASHCAP_HOST", "127.0.0.1").strip() or "127.0.0.1"


def get_public_url() -> str:
    """Return the externally visible origin, if behind a proxy.

    Tailscale Serve forwards HTTP to Flask, so ``request.host_url`` alone would
    inject ``http://…`` into the userscript even when the browser is on HTTPS.
    """
    return os.getenv("FLASHCAP_PUBLIC_URL", "").strip().rstrip("/")


def get_port() -> int:
    """Return the configured local port."""
    try:
        return int(os.getenv("FLASHCAP_PORT", "8765"))
    except ValueError:
        return 8765


def get_desired_retention() -> float:
    """FSRS desired retention, clamped to a sane range."""
    try:
        value = float(os.getenv("FLASHCAP_DESIRED_RETENTION", "0.9"))
    except ValueError:
        value = 0.9
    return min(0.99, max(0.70, value))


def get_fsrs_fuzzing() -> bool:
    """Whether FSRS may fuzz intervals to avoid same-day clumps."""
    raw = os.getenv("FLASHCAP_FSRS_FUZZING", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def get_note_context() -> bool:
    """Whether reading notes include a surrounding-context callout.

    Off by default: the highlight and annotation are the note; a 1,400-char
    context block is usually noise. Set FLASHCAP_NOTE_CONTEXT=1 to include a
    short excerpt.
    """
    raw = os.getenv("FLASHCAP_NOTE_CONTEXT", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def get_db_path() -> str:
    """Return the configured SQLite path."""
    configured = os.getenv("FLASHCAP_DB", "").strip()
    if configured:
        return str(Path(configured).expanduser())
    return str(CAPTURE_DIR / "flashcards.db")


def get_telegram_config() -> dict[str, object]:
    """Return the Telegram long-poll settings.

    Ingestion stays off until a bot token exists, so a fresh checkout and the
    test suite never touch the network.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    raw_chats = os.getenv("TELEGRAM_CHAT_IDS", "")
    chat_ids = tuple(part.strip() for part in raw_chats.split(",") if part.strip())
    poll = os.getenv("FLASHCAP_TELEGRAM_POLL", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    try:
        timeout = int(os.getenv("FLASHCAP_TELEGRAM_TIMEOUT", "25"))
    except ValueError:
        timeout = 25
    return {
        "enabled": bool(token) and poll,
        "token": token,
        "chat_ids": chat_ids,
        "timeout": max(1, min(timeout, 50)),
    }


def get_obsidian_config() -> dict[str, object]:
    """Return the Obsidian reading-note settings.

    Notes are written through Obsidian's built-in URI handler, so there is no
    vault path to manage. ``Writing``/``Reading`` are the defaults for this
    machine; override them in ``.env`` to use another vault or folder.
    """
    enabled = os.getenv("FLASHCAP_OBSIDIAN_ENABLED", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    vault = os.getenv("FLASHCAP_OBSIDIAN_VAULT", "Writing").strip()
    folder = os.getenv("FLASHCAP_OBSIDIAN_FOLDER", "Reading").strip().strip("/")
    mode = os.getenv("FLASHCAP_OBSIDIAN_MODE", "uri").strip().lower()
    if mode not in {"uri", "queue"}:
        mode = "uri"
    return {
        "enabled": enabled and bool(vault),
        "vault": vault,
        "folder": folder or "Reading",
        "mode": mode,
    }
