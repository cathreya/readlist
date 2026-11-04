#!/usr/bin/env python3
"""Flush queued Obsidian note URIs from a remote Flashcap server.

Runs on the Mac (the only machine that can hand ``obsidian://`` URIs to
Obsidian). Designed for a launchd StartInterval job; it does one drain pass and
exits. It never stores note content locally: the server queues the exact URI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REMOTE = os.getenv("FLASHCAP_REMOTE_URL", "https://hermes.taila17c7c.ts.net").rstrip("/")
DELAY = float(os.getenv("FLASHCAP_AGENT_DELAY", "1.2"))
MAX_URI_CHARS = 100_000
ALLOWED_URI_PREFIXES = ("obsidian://new?", "obsidian://open?")


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{REMOTE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def pending() -> list[dict]:
    return _request("GET", "/api/obsidian/pending").get("notes", [])


def ack(queue_id: int) -> None:
    _request("POST", "/api/obsidian/ack", {"id": queue_id})


def _safe_uri(uri: str) -> bool:
    """Only let the server hand us Obsidian note URIs, nothing else."""
    if not isinstance(uri, str) or len(uri) > MAX_URI_CHARS:
        return False
    if not uri.startswith(ALLOWED_URI_PREFIXES):
        return False
    parsed = urllib.parse.urlsplit(uri)
    return parsed.scheme == "obsidian" and parsed.netloc in ("new", "open")


def _safe_path(path: str) -> bool:
    """Keep generated notes relative and inside the vault."""
    if not path or path.startswith("/"):
        return False
    return ".." not in path.replace("\\", "/").split("/")


def drain() -> int:
    notes = pending()
    if not notes:
        print("note agent: queue empty")
        return 0

    opened = 0
    for note in notes:
        queue_id = note.get("id")
        uri = note.get("uri")
        if queue_id is None or not uri:
            continue

        if not _safe_uri(uri) or not _safe_path(str(note.get("path") or "")):
            print(
                f"note agent: refusing unexpected URI for {queue_id}",
                file=sys.stderr,
            )
            ack(queue_id)
            continue

        try:
            result = subprocess.run(
                ["open", "-g", uri],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"note agent: could not open {queue_id}: {exc}", file=sys.stderr)
            break

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            print(
                f"note agent: open exited {result.returncode} for {queue_id}: {detail}",
                file=sys.stderr,
            )
            break

        # Obsidian processes URIs asynchronously; keep appends in order.
        time.sleep(DELAY)
        ack(queue_id)
        opened += 1
        print(f"note agent: delivered {note.get('path') or queue_id}")

    return opened


def main() -> int:
    if sys.platform != "darwin":
        print("note agent: this helper is macOS-only", file=sys.stderr)
        return 1
    try:
        drain()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
        print(f"note agent: remote unavailable: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
