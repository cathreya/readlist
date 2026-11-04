"""Parse Telegram-exported messages.html to extract all links with surrounding context.

Usage:
    python -m app.parser
"""

import os
import re
import logging
from collections import Counter
from html import unescape
from typing import Optional
from urllib.parse import urlparse, unquote

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Domain normalisation mapping ─────────────────────────────────────
# Longer / more-specific entries should come first so that endswith
# checks don't short-circuit to a generic match.
DOMAIN_MAP = {
    "en.wikipedia.org": "wikipedia",
    "news.ycombinator.com": "hackernews",
    "open.spotify.com": "spotify",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "arxiv.org": "arxiv",
    "github.com": "github",
    "amazon.com": "amazon",
    "amzn.eu": "amazon",
    "a.co": "amazon",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "t.co": "twitter",
    "reddit.com": "reddit",
    "substack.com": "substack",
    "medium.com": "medium",
    "instagram.com": "instagram",
    "discord.com": "discord",
    "discord.gg": "discord",
}

# Known domain -> content_type overrides.
CONTENT_TYPE_DOMAIN = {
    "youtube": "video",
    "arxiv": "paper",
    "github": "repo",
    "amazon": "book",
    "twitter": "social",
    "reddit": "social",
    "hackernews": "social",
    "spotify": "music",
}


def normalize_domain(url: str) -> str:
    """Normalise a URL's domain to a short canonical name (e.g. "youtube")."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
    except Exception:
        return "unknown"
    if domain.startswith("www."):
        domain = domain[4:]
    for key, normalized in DOMAIN_MAP.items():
        if domain == key or domain.endswith("." + key):
            return normalized
    return domain


def detect_content_type(url: Optional[str], domain: str) -> str:
    """Infer content type from domain and URL path."""
    if domain in CONTENT_TYPE_DOMAIN:
        return CONTENT_TYPE_DOMAIN[domain]
    if url:
        path = urlparse(url).path.lower()
        if path.endswith(".pdf"):
            return "pdf"
        if path.endswith(".epub"):
            return "book"
        if path.endswith((".htm", ".html")):
            return "article"
    return "article"


def _file_content_type(filename: str) -> str:
    """Map a filename to a content type based on its extension."""
    ext = os.path.splitext(filename)[1].lower()
    return {
        ".pdf": "pdf",
        ".epub": "book",
        ".mobi": "book",
        ".htm": "article",
        ".html": "article",
        ".docx": "document",
        ".doc": "document",
        ".txt": "document",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".gif": "image",
    }.get(ext, "document")


# ── URL helpers ──────────────────────────────────────────────────────

_URL_RE = re.compile(r"https?://[^\s<>\"'()]+")


def extract_urls_from_text(text: str) -> list[str]:
    """Extract http/https URLs from raw text via regex (fallback path)."""
    return _URL_RE.findall(text)


def extract_title_from_anchor(a_tag) -> Optional[str]:
    """Return descriptive anchor text, or *None* if it's just the bare URL."""
    text = a_tag.get_text(strip=True)
    if not text:
        return None
    href = a_tag.get("href", "").strip()
    if text == href or text.startswith("http"):
        return None
    return text


def clean_url(url: str) -> str:
    """Strip trailing punctuation that sometimes gets glued to URLs."""
    return url.rstrip(".,;:!?\"')")


# ── File-attachment helpers ──────────────────────────────────────────

_PROJECT_ROOT: Optional[str] = None


def _get_project_root() -> str:
    """Return the absolute path of the project root (parent of ``app/``)."""
    global _PROJECT_ROOT
    if _PROJECT_ROOT is None:
        _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _PROJECT_ROOT


def build_files_index(files_dir: str) -> dict[str, str]:
    """Build a case-insensitive filename -> absolute-path mapping.

    Auto-generated Telegram thumbnails (``*_thumb.jpg``) are excluded.
    """
    if not os.path.isdir(files_dir):
        logger.warning("Files directory not found: %s", files_dir)
        return {}
    index: dict[str, str] = {}
    for fname in os.listdir(files_dir):
        if "_thumb.jpg" in fname:
            continue
        index[fname.lower()] = os.path.join(files_dir, fname)
    return index


def extract_file_attachments(
    body_div, html_dir: str, files_index: dict[str, str]
) -> list[str]:
    """Extract absolute paths to file attachments in a message body.

    Handles two Telegram-export patterns:

    1. ``<a class="... media_file" href="files/...">`` (file available)
    2. ``<div class="... media_file">`` with ``.title.bold`` (file unavailable in
       chat, but may still be on disk from a previous download).
    """
    paths: list[str] = []

    # Pattern 1: linked file attachment (block_link media_file).
    # NOTE: we intentionally use find_all() rather than select() because
    # Telegram-exported HTML nests a second <div class="body"> inside the
    # <a>, and select() can behave unexpectedly with that nesting.
    for a_tag in body_div.find_all("a", class_="media_file"):
        href = a_tag.get("href", "")
        if not href:
            continue
        href = unescape(unquote(href))
        full = os.path.normpath(os.path.join(html_dir, href))
        if os.path.isfile(full):
            paths.append(full)
            continue
        # Fall back to case-insensitive filename lookup.
        fname = os.path.basename(href)
        match = files_index.get(fname.lower())
        if match:
            paths.append(match)

    # Pattern 2: unlinked file attachment (media_file div, file unavailable).
    for div_tag in body_div.find_all("div", class_="media_file"):
        title_div = div_tag.find("div", class_="title bold")
        if not title_div:
            continue
        fname = title_div.get_text(strip=True)
        match = files_index.get(fname.lower())
        if match:
            paths.append(match)

    return paths


# ── Main parsing entry point ─────────────────────────────────────────

def parse_messages(
    html_path: Optional[str] = None,
    files_dir: Optional[str] = None,
) -> list[dict]:
    """Parse ``messages.html`` and return a list of extracted items.

    Each item is a dict with keys:

    * ``url``             — extracted URL, or ``None``
    * ``title``           — descriptive link text (if different from the URL)
    * ``domain``          — normalised domain label
    * ``content_type``    — ``video``, ``paper``, ``article``, ``pdf``, ``book``,
                            ``repo``, ``social``, ``music``, ``note``, etc.
    * ``description``     — surrounding message text
    * ``local_file_path``  — absolute path to a matched local file, or ``None``

    URLs are deduplicated across messages (first occurrence wins).
    """
    if html_path is None:
        html_path = os.path.join(_get_project_root(), "messages.html")
    if files_dir is None:
        files_dir = os.path.join(_get_project_root(), "files")

    files_index = build_files_index(files_dir)
    html_dir = os.path.dirname(os.path.abspath(html_path))

    # ── Read HTML ────────────────────────────────────────────────────
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()
    except FileNotFoundError:
        logger.error("File not found: %s", html_path)
        return []
    except Exception as e:
        logger.error("Error reading %s: %s", html_path, e)
        return []

    # ── Parse ────────────────────────────────────────────────────────
    try:
        soup = BeautifulSoup(html_content, "html.parser")
    except Exception as e:
        logger.error("Error parsing HTML: %s", e)
        return []

    message_divs = soup.select("div.message.default.clearfix")
    seen_urls: set[str] = set()
    results: list[dict] = []

    for msg_div in message_divs:
        body_div = msg_div.find("div", class_="body")
        if not body_div:
            continue

        # ── 1. Extract text content ────────────────────────────────
        # The first .text div anywhere inside .body handles both
        # direct messages and forwarded messages (which nest their
        # .text inside .forwarded.body).
        text_div = body_div.find("div", class_="text")
        full_text = (
            text_div.get_text(separator=" ", strip=True) if text_div else ""
        )

        # ── 2. Extract URLs from <a> tags within .text ────────────
        message_urls: list[str] = []
        url_titles: dict[str, str] = {}

        if text_div:
            for a_tag in text_div.find_all("a", href=True):
                href = a_tag["href"].strip()
                if not (href.startswith("http://") or href.startswith("https://")):
                    continue
                cleaned = clean_url(href)
                if cleaned and cleaned not in seen_urls:
                    seen_urls.add(cleaned)
                    message_urls.append(cleaned)
                    title = extract_title_from_anchor(a_tag)
                    if title:
                        url_titles[cleaned] = title

        # Fallback: regex for bare URLs in text (rare in Telegram exports).
        if text_div:
            for raw_url in extract_urls_from_text(full_text):
                cleaned = clean_url(raw_url)
                if cleaned and cleaned not in seen_urls and cleaned not in message_urls:
                    seen_urls.add(cleaned)
                    message_urls.append(cleaned)

        # ── 3. Extract file attachments ───────────────────────────
        file_paths = extract_file_attachments(body_div, html_dir, files_index)

        # ── 4. Create items for each URL ───────────────────────────
        remaining_files = list(file_paths)
        for url in message_urls:
            domain = normalize_domain(url)
            content_type = detect_content_type(url, domain)
            result: dict = {
                "url": url,
                "title": url_titles.get(url),
                "domain": domain,
                "content_type": content_type,
                "description": full_text or None,
                "local_file_path": None,
            }
            # Associate one file attachment with this URL if available.
            if remaining_files:
                result["local_file_path"] = remaining_files.pop(0)
            results.append(result)

        # ── 5. Create items for leftover file-only messages ────────
        for fp in remaining_files:
            fname = os.path.basename(fp)
            results.append({
                "url": None,
                "title": fname,
                "domain": None,
                "content_type": _file_content_type(fname),
                "description": full_text or None,
                "local_file_path": fp,
            })

        # ── 6. Plain notes (no URLs, no files) ─────────────────────
        stripped = full_text.strip()
        if not message_urls and not file_paths and stripped:
            results.append({
                "url": None,
                "title": None,
                "domain": None,
                "content_type": "note",
                "description": stripped,
                "local_file_path": None,
            })

    return results


# Alias for app.py compatibility
parse_messages_html = parse_messages



def summary_stats(items: list[dict]) -> dict:
    """Compute summary statistics over the parsed items."""
    total = len(items)
    with_url = [i for i in items if i["url"]]
    domains = Counter(i["domain"] for i in items if i["domain"])
    types = Counter(i["content_type"] for i in items)
    return {
        "total_messages": total,
        "total_links": len(with_url),
        "unique_domains": len(domains),
        "domains": domains.most_common(),
        "content_type_breakdown": types.most_common(),
        "file_attachments": sum(1 for i in items if i["local_file_path"]),
        "plain_notes": sum(1 for i in items if i["content_type"] == "note"),
    }


def print_summary(items: list[dict]) -> None:
    """Print a human-readable summary to stdout."""
    stats = summary_stats(items)
    print(f"  Total messages / items extracted:  {stats['total_messages']}")
    print(f"  Items with URLs (links):           {stats['total_links']}")
    print(f"  Unique domains:                    {stats['unique_domains']}")
    print(f"  File attachments matched:          {stats['file_attachments']}")
    print(f"  Plain notes (no link/file):        {stats['plain_notes']}")
    print()
    print("  Content-type breakdown:")
    for ctype, count in stats["content_type_breakdown"]:
        print(f"    {ctype:>12s}: {count}")
    print()
    print("  Top domains:")
    for domain, count in stats["domains"][:10]:
        print(f"    {domain:>20s}: {count}")


# ── CLI entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    items = parse_messages()
    print(f"\nParsed {len(items)} items from messages.html\n")
    if items:
        print_summary(items)
    else:
        print("(no items extracted)")
    print()
