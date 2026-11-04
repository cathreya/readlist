"""Fetch metadata for extracted links to enrich before LLM classification.

Supports specialised metadata sources for YouTube, arXiv, and Wikipedia,
with a generic fallback for all other URLs.

Usage:
    python -m app.metadata items.json [output.json]
"""

import json
import logging
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Callable, Optional
from urllib.parse import urlparse, quote

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

REQUESTS_TIMEOUT = 10  # seconds
GENERAL_RATE_LIMIT = 1.0  # minimum seconds between general HTTP requests

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ── Rate limiter ─────────────────────────────────────────────────────

_last_request_time: float = 0.0


def _rate_limit(delay: float = GENERAL_RATE_LIMIT) -> None:
    """Block until at least *delay* seconds have passed since the last call."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_request_time = time.monotonic()


# ── YouTube helpers ──────────────────────────────────────────────────

def _extract_youtube_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from common URL formats."""
    parsed = urlparse(url)
    # youtu.be/VIDEO_ID
    if "youtu.be" in parsed.netloc:
        return parsed.path.strip("/").split("?")[0].split("&")[0]
    # youtube.com/watch?v=VIDEO_ID
    if "youtube.com" in parsed.netloc:
        if "watch" in parsed.path:
            match = re.search(r"[?&]v=([^&]+)", url)
            if match:
                return match.group(1)
        # youtube.com/embed/VIDEO_ID
        if "embed" in parsed.path:
            return parsed.path.split("/")[-1]
    return None


def _parse_iso8601_duration(duration: str) -> Optional[int]:
    """Parse ISO 8601 duration (e.g. PT1H30M15S) into total minutes."""
    if not duration:
        return None
    match = re.match(
        r"^PT?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", duration
    )
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 60 + minutes + (1 if seconds >= 30 else 0)


def _fetch_youtube_api(video_id: str) -> Optional[dict]:
    """Fetch YouTube metadata via the Data API v3."""
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        logger.debug("YOUTUBE_API_KEY not set, skipping YouTube API")
        return None

    _rate_limit(0.5)
    url = (
        "https://www.googleapis.com/youtube/v3/videos"
        f"?id={video_id}&key={api_key}&part=snippet,contentDetails"
    )
    try:
        resp = requests.get(url, timeout=REQUESTS_TIMEOUT)
        if resp.status_code == 403:
            logger.warning("YouTube API key quota exceeded or invalid")
            return None
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items:
            return None
        snippet = items[0].get("snippet", {})
        content = items[0].get("contentDetails", {})
        duration_min = _parse_iso8601_duration(content.get("duration", ""))
        return {
            "title": snippet.get("title"),
            "description": snippet.get("description"),
            "duration_minutes": duration_min,
        }
    except requests.RequestException as e:
        logger.warning("YouTube API request failed: %s", e)
        return None


def _fetch_youtube_ytdlp(video_id: str, url: str) -> Optional[dict]:
    """Fetch YouTube metadata via yt-dlp subprocess."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--print", "title", "--print", "duration", url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug("yt-dlp exited with code %d: %s",
                         result.returncode, result.stderr.strip())
            return None
        lines = result.stdout.strip().split("\n")
        title = lines[0] if lines else None
        duration_str = lines[1] if len(lines) > 1 else None
        duration_min = int(duration_str) // 60 if duration_str and duration_str.isdigit() else None
        return {
            "title": title,
            "description": None,
            "duration_minutes": duration_min,
        }
    except FileNotFoundError:
        logger.debug("yt-dlp not installed")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("yt-dlp timed out for %s", url)
        return None
    except Exception as e:
        logger.warning("yt-dlp failed for %s: %s", url, e)
        return None


# ── arXiv helper ─────────────────────────────────────────────────────

def _extract_arxiv_id(url: str) -> Optional[str]:
    """Extract the arXiv ID from an arxiv.org URL."""
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    # Patterns: abs/2106.02026, pdf/2106.02026.pdf, 2106.02026
    match = re.search(r"(?:abs|pdf)/(\d+\.\d+)", path)
    if match:
        return match.group(1)
    # Bare ID in path
    match = re.search(r"^(\d+\.\d+)", path)
    if match:
        return match.group(1)
    return None


def _fetch_arxiv_metadata(arxiv_id: str) -> dict:
    """Fetch metadata from the arXiv API for a given paper ID."""
    _rate_limit(0.5)
    url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}&max_results=1"
    try:
        resp = requests.get(url, timeout=REQUESTS_TIMEOUT)
        if resp.status_code >= 400:
            return {
                "title": None, "description": None, "duration_minutes": None,
                "error": _classify_http_error(resp.status_code),
            }
        root = ET.fromstring(resp.content)
        # Namespace handling
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entry = root.find("atom:entry", ns)
        if entry is None:
            return {"title": None, "description": None, "duration_minutes": None}
        title_el = entry.find("atom:title", ns)
        summary_el = entry.find("atom:summary", ns)
        title = title_el.text.strip() if title_el is not None and title_el.text else None
        summary = summary_el.text.strip() if summary_el is not None and summary_el.text else None
        # API returns title with newlines; collapse them.
        if title:
            title = re.sub(r"\s+", " ", title)
        if summary:
            summary = re.sub(r"\s+", " ", summary)
        return {
            "title": title,
            "description": summary,
            "duration_minutes": None,
        }
    except ET.ParseError as e:
        logger.warning("arXiv API returned unparseable XML for %s: %s", arxiv_id, e)
    except requests.RequestException as e:
        logger.warning("arXiv API request failed for %s: %s", arxiv_id, e)
    return {"title": None, "description": None, "duration_minutes": None, "error": "api_error"}


# ── Wikipedia helper ─────────────────────────────────────────────────

def _extract_wikipedia_title(url: str) -> Optional[str]:
    """Extract the page title from a Wikipedia URL."""
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if path.startswith("wiki/"):
        return path[5:]
    return path


def _fetch_wikipedia_metadata(page_title: str) -> dict:
    """Fetch metadata from the Wikipedia REST API."""
    _rate_limit(0.3)
    encoded = quote(page_title, safe="")
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}"
    try:
        resp = requests.get(url, timeout=REQUESTS_TIMEOUT)
        if resp.status_code == 404:
            logger.debug("Wikipedia page not found: %s", page_title)
            return {"title": None, "description": None, "duration_minutes": None}
        if resp.status_code >= 400:
            return {
                "title": None, "description": None, "duration_minutes": None,
                "error": _classify_http_error(resp.status_code),
            }
        data = resp.json()
        return {
            "title": data.get("title"),
            "description": data.get("extract"),
            "duration_minutes": None,
        }
    except requests.RequestException as e:
        logger.warning("Wikipedia API request failed for %s: %s", page_title, e)
        return {"title": None, "description": None, "duration_minutes": None,
                "error": "api_error"}


# ── Generic URL helper ───────────────────────────────────────────────

def _fetch_generic_metadata(url: str) -> dict:
    """Fetch metadata from a generic web page via Open Graph and HTML tags."""
    _rate_limit(GENERAL_RATE_LIMIT)
    try:
        resp = requests.get(
            url,
            timeout=REQUESTS_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
        status = resp.status_code

        # Detect dead or auth-gated pages.
        if status >= 400:
            error_reason = _classify_http_error(status)
            return {
                "title": None,
                "description": None,
                "duration_minutes": None,
                "error": error_reason,
            }

        # Abort on non-HTML responses (PDFs, images, etc.) to avoid
        # wasting bandwidth on parsing.
        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            return {"title": None, "description": None, "duration_minutes": None}

        soup = BeautifulSoup(resp.content, "html.parser")

        # Try og:title, then twitter:title, then <title>.
        title = (
            _meta_content(soup, "og:title")
            or _meta_content(soup, "twitter:title")
            or _meta_name(soup, "title")
        )
        if not title:
            title_tag = soup.find("title")
            if title_tag and title_tag.text:
                title = title_tag.text.strip()

        # Try og:description, then meta description, then twitter:description.
        description = (
            _meta_content(soup, "og:description")
            or _meta_name(soup, "description")
            or _meta_content(soup, "twitter:description")
        )

        return {
            "title": title,
            "description": description,
            "duration_minutes": None,
            "error": None,
        }

    except requests.Timeout:
        logger.debug("Timeout fetching %s", url)
        return {"title": None, "description": None, "duration_minutes": None,
                "error": "timeout"}
    except requests.ConnectionError:
        logger.debug("Connection error fetching %s", url)
        return {"title": None, "description": None, "duration_minutes": None,
                "error": "connection_error"}
    except requests.RequestException as e:
        logger.warning("Request failed for %s: %s", url, e)
        return {"title": None, "description": None, "duration_minutes": None,
                "error": "request_failed"}


def _meta_content(soup, property_value: str) -> Optional[str]:
    """Extract ``content`` from a ``<meta property=\"...\">`` or ``<meta name=\"...\">`` tag."""
    tag = (
        soup.find("meta", attrs={"property": property_value})
        or soup.find("meta", attrs={"name": property_value})
    )
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def _meta_name(soup, name_value: str) -> Optional[str]:
    """Extract ``content`` from ``<meta name=\"...\" content=\"...\">``."""
    tag = soup.find("meta", attrs={"name": name_value})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


# ── HTTP error classification ────────────────────────────────────────

def _classify_http_error(status: int) -> str:
    """Classify an HTTP status code into a human-readable reason string."""
    if status in (401, 403):
        return "auth_required"
    if status == 404:
        return "not_found"
    if status == 410:
        return "gone"
    if status == 429:
        return "rate_limited"
    if 500 <= status < 600:
        return "server_error"
    return f"http_{status}"


# ── Public API ───────────────────────────────────────────────────────

def fetch_metadata(url: str, domain: str, content_type: str) -> dict:
    """Fetch metadata for a single URL.

    Returns a dict with keys:

    * ``title``            — page / video / paper title, or ``None``
    * ``description``      — page description / abstract, or ``None``
    * ``duration_minutes`` — duration in minutes (mainly for video), or ``None``
    * ``error``            — error string if the fetch failed, or ``None``
    """
    result: dict = {
        "title": None,
        "description": None,
        "duration_minutes": None,
        "error": None,
    }

    # ── YouTube ────────────────────────────────────────────────────
    if domain == "youtube":
        video_id = _extract_youtube_id(url)
        if video_id:
            meta = _fetch_youtube_api(video_id)
            if meta is None:
                meta = _fetch_youtube_ytdlp(video_id, url)
            if meta:
                result.update(meta)
        return result

    # ── arXiv ──────────────────────────────────────────────────────
    if content_type == "paper" or domain == "arxiv":
        arxiv_id = _extract_arxiv_id(url)
        if arxiv_id:
            meta = _fetch_arxiv_metadata(arxiv_id)
            result.update(meta)
        return result

    # ── Wikipedia ───────────────────────────────────────────────────
    if domain == "wikipedia":
        title = _extract_wikipedia_title(url)
        if title:
            meta = _fetch_wikipedia_metadata(title)
            result.update(meta)
        return result

    # ── PDF (no HTML to scrape) ──────────────────────────────────
    if content_type in ("pdf",):
        return result

    # ── Generic web page ────────────────────────────────────────────
    meta = _fetch_generic_metadata(url)
    result.update(meta)
    return result


def fetch_all(
    items: list[dict],
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Batch-fetch metadata for every item in *items*.

    Each item dict is enriched with ``fetched_title``, ``fetched_description``,
    ``duration_minutes`` and ``fetch_error`` keys derived from the URL's page.
    Fetched data takes precedence when the parser did not extract its own
    ``title`` or ``description``.

    Individual fetch failures are caught and recorded in ``fetch_error`` —
    the batch never crashes on a single bad URL.
    """
    total = len(items)
    enriched: list[dict] = []

    for idx, item in enumerate(items):
        url = item.get("url")
        if not url:
            # Items without a URL (notes, unmatched files) get pass-through.
            enriched_item = dict(item)
            enriched_item["fetched_title"] = None
            enriched_item["fetched_description"] = None
            enriched_item["duration_minutes"] = None
            enriched_item["fetch_error"] = None
            enriched.append(enriched_item)
            if progress_callback:
                progress_callback(idx + 1, total)
            continue

        meta = fetch_metadata(
            url,
            item.get("domain", ""),
            item.get("content_type", "article"),
        )

        enriched_item = dict(item)
        enriched_item["fetched_title"] = meta.get("title")
        enriched_item["fetched_description"] = meta.get("description")
        enriched_item["duration_minutes"] = meta.get("duration_minutes")
        enriched_item["fetch_error"] = meta.get("error")

        # Merge: prefer fetched title/description over parser's when
        # the parser had nothing useful.
        if meta.get("title") and not item.get("title"):
            enriched_item["title"] = meta["title"]
        if meta.get("description") and not item.get("description"):
            enriched_item["description"] = meta["description"]

        enriched.append(enriched_item)

        if progress_callback:
            progress_callback(idx + 1, total)

    return enriched


# ── CLI entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    if len(sys.argv) < 2:
        print("Usage: python -m app.metadata <items.json> [output.json]")
        print("")
        print("  items.json    JSON array of items from app.parser")
        print("  output.json   Optional output path (default: stdout)")
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        with open(input_path, "r", encoding="utf-8") as f:
            items = json.load(f)
    except FileNotFoundError:
        logger.error("Input file not found: %s", input_path)
        sys.exit(1)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s: %s", input_path, e)
        sys.exit(1)

    logger.info("Enriching %d items with metadata ...", len(items))

    def _print_progress(count: int, total: int) -> None:
        if total > 0 and count % max(1, total // 20) == 0:
            logger.info("  Progress: %d / %d (%.0f%%)", count, total, count / total * 100)

    enriched = fetch_all(items, progress_callback=_print_progress)

    # Count successes / failures
    ok = sum(1 for e in enriched if e.get("fetch_error") is None and e.get("url"))
    failed = sum(1 for e in enriched if e.get("fetch_error"))
    skipped = sum(1 for e in enriched if not e.get("url"))
    titles = sum(1 for e in enriched if e.get("fetched_title"))

    logger.info(
        "Done: %d fetched, %d titles obtained, %d failed, %d skipped (notes/files).",
        ok, titles, failed, skipped,
    )

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(enriched, f, indent=2, ensure_ascii=False)
        logger.info("Saved to %s", output_path)
    else:
        print(json.dumps(enriched, indent=2, ensure_ascii=False))
