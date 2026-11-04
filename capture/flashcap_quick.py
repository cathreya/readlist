#!/usr/bin/env python3
"""Open Flashcap's quick-capture page with a passage already filled in.

Firefox's PDF viewer and Reader View are privileged pages, so the userscript
cannot run inside them (no hotkey, no overlay menu). This is the out-of-page
path: a macOS Shortcut or Service hands over the text you just selected, and
this script opens ``/quick?text=...`` so you write the card as usual.

It never watches the clipboard and never runs on its own -- it only does
anything when something (a Shortcut, a Service, a shell pipe) invokes it.

Usage
-----
    pbpaste | flashcap_quick.py
    flashcap_quick.py --clipboard
    flashcap_quick.py --text "the passage" --title "Paper" --url https://...
    flashcap_quick.py --print --text "..."     # show the URL, open nothing

The text argument is read from stdin when it is not a terminal, which is how
macOS passes a Service's selection to a shell script.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.parse

# Matches the bookmarklet's cap on quick.js (and stays well under URL limits).
MAX_TEXT = 12000
DEFAULT_BASE = os.environ.get("FLASHCAP_URL", "http://127.0.0.1:8765").rstrip("/")


def build_url(
    text: str,
    *,
    base: str = DEFAULT_BASE,
    title: str = "",
    url: str = "",
    hint: str = "",
) -> str:
    """Return the quick-capture URL for ``text``.

    Raises ``ValueError`` when there is no passage to capture.
    """
    passage = (text or "").strip()
    if not passage:
        raise ValueError("no text to capture")

    params = [("text", passage[:MAX_TEXT])]
    for key, value in (("title", title), ("url", url), ("hint", hint)):
        value = (value or "").strip()
        if value:
            params.append((key, value))

    query = urllib.parse.urlencode(params)
    return f"{base.rstrip('/')}/quick?{query}"


def read_clipboard() -> str:
    """Return the macOS clipboard as text (empty string when it is not text)."""
    result = subprocess.run(["pbpaste"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open Flashcap quick capture with a passage prefilled.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="passage to capture")
    source.add_argument(
        "--clipboard",
        action="store_true",
        help="read the passage from the clipboard (explicit, on demand)",
    )
    parser.add_argument("--title", default="", help="source title")
    parser.add_argument("--url", default="", help="source URL")
    parser.add_argument("--hint", default="", help="category hint for the suggester")
    parser.add_argument("--base", default=DEFAULT_BASE, help="Flashcap origin")
    parser.add_argument(
        "--print",
        dest="dry_run",
        action="store_true",
        help="print the URL instead of opening a browser",
    )
    args = parser.parse_args(argv)

    if args.text is not None:
        text = args.text
    elif args.clipboard:
        text = read_clipboard()
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.error("pass --text, --clipboard, or pipe the passage on stdin")

    try:
        target = build_url(
            text,
            base=args.base,
            title=args.title,
            url=args.url,
            hint=args.hint,
        )
    except ValueError:
        print("flashcap: nothing selected or the clipboard held no text.", file=sys.stderr)
        return 2

    if args.dry_run:
        print(target)
        return 0

    opened = subprocess.run(["open", target], capture_output=True, text=True)
    if opened.returncode != 0:
        print(f"flashcap: could not open the browser: {opened.stderr.strip()}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
