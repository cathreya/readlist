#!/usr/bin/env python3
"""Classification pipeline CLI for Lecture Today.

Runs the full classification pipeline on all unclassified items:

  1. classify_items         — LLM assigns topic, difficulty, estimated_minutes, blurb
  2. assign_prerequisites   — LLM infers prerequisite relationships per topic group
  3. chunk_items            — Split long items into chunked child items by content type
  4. pre_generate_lectures  — Generate warmup paragraphs and guided questions

Usage:
    python classifier.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

try:
    from openai import OpenAI
except ImportError:
    print("Error: openai package not installed. Run: pip install openai")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

_COLUMN_ADDITIONS = [
    "ALTER TABLE items ADD COLUMN prerequisite_topics TEXT",
]


def _ensure_columns(db: sqlite3.Connection) -> None:
    """Add columns that may not exist in older schemas."""
    for sql in _COLUMN_ADDITIONS:
        try:
            db.execute(sql)
        except sqlite3.OperationalError:
            pass
    db.commit()


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def call_llm(
    model_config: dict[str, str],
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    max_tokens: int = 2000,
) -> str:
    """Call an OpenAI-compatible LLM and return the response text.

    Raises on network errors, non-200 status, or empty response.
    """
    client = OpenAI(
        api_key=model_config["api_key"],
        base_url=model_config["base_url"],
    )
    response = client.chat.completions.create(
        model=model_config["model"],
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=90,
    )
    body = response.choices[0].message.content
    if body is None or body.strip() == "":
        raise ValueError("LLM returned empty response")
    return body.strip()


def _parse_json(text: str) -> Any:
    """Extract JSON from response text, handling markdown fences."""
    text = text.strip()
    # Strip markdown code fences
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{") or part.startswith("["):
                text = part
                break
    return json.loads(text)


# ---------------------------------------------------------------------------
# Default progress callback
# ---------------------------------------------------------------------------

def _default_progress(
    stage: str, current: int, total: int, detail: str | None = None
) -> None:
    """Print a progress line to stdout with carriage-return overwrite."""
    if total == 0:
        print(f"  {stage}: nothing to do.")
        return
    pct = current / total * 100 if total else 0
    msg = f"  {stage}... {current}/{total} ({pct:.0f}%)"
    if detail:
        msg += f"  [{detail}]"
    if current >= total:
        print(f"\r{msg}")
    else:
        print(f"\r{msg}", end="", flush=True)


# ---------------------------------------------------------------------------
# Stage 1: classify_items
# ---------------------------------------------------------------------------

_CLASSIFY_SYSTEM = (
    "You are a classification assistant. Return ONLY valid JSON. "
    "Do NOT include markdown fences or extra text."
)

_CLASSIFY_PROMPT_TPL = """\
You are classifying a saved item for a personal reading list. Given the metadata below, return a JSON object with these fields. Do NOT fabricate content — work only from what's provided.

Metadata: title={title}, description={description}, domain={domain}, type={content_type}

Return ONLY this JSON:
{{
  "topic": "<one of: machine-learning, algorithms, systems, math, physics, history, philosophy, programming, neuroscience, economics, biology, ireland, india, design, career, other>",
  "estimated_minutes": <integer, 5-120>,
  "difficulty": <integer 1-5. 1=light blog post, 2=accessible explainer, 3=requires focus, 4=technical/dense, 5=graduate-level or textbook>,
  "prerequisite_topics": [<list of topic strings that would help to know first, e.g. "linear-algebra">],
  "blurb": "<one sentence on why this is worth reading/watching>",
  "needs_chunking": <true if estimated_minutes > 60>,
  "confidence": <0.0-1.0, how confident you are in this classification>
}}"""


def classify_items(
    db: sqlite3.Connection,
    model_config: dict[str, str],
    progress_callback: Callable | None = None,
) -> dict[str, int]:
    """Stage 1: Classify all unread items without a difficulty rating.

    Calls the LLM (5 concurrent workers) for each item and stores:
    topic, estimated_minutes, difficulty, blurb, prerequisite_topics.
    Items with confidence < 0.7 are flagged with ``error_reason`` but
    their classification data is still persisted.

    Returns a dict with keys: total, succeeded, failed, low_confidence.
    """
    if progress_callback is None:
        progress_callback = _default_progress

    _ensure_columns(db)

    rows = db.execute(
        "SELECT id, title, description, domain, content_type FROM items "
        "WHERE status = 'unread' AND difficulty IS NULL"
    ).fetchall()

    total = len(rows)
    if total == 0:
        progress_callback("Classifying", 0, 0)
        return {"total": 0, "succeeded": 0, "failed": 0, "low_confidence": 0}

    succeeded = failed = low_confidence = 0

    def _classify_one(item: sqlite3.Row) -> dict[str, Any]:
        item_id = item["id"]
        title = item["title"] or ""
        description = item["description"] or ""
        domain = item["domain"] or ""
        content_type = item["content_type"] or ""

        user_prompt = _CLASSIFY_PROMPT_TPL.format(
            title=title,
            description=description,
            domain=domain,
            content_type=content_type,
        )

        last_error: str | None = None
        for attempt in range(2):
            try:
                raw = call_llm(
                    model_config, _CLASSIFY_SYSTEM, user_prompt, temperature=0.3
                )
                data = _parse_json(raw)

                topic = str(data.get("topic", "other"))
                estimated_minutes = int(data.get("estimated_minutes", 30))
                difficulty = int(data.get("difficulty", 3))
                blurb = str(data.get("blurb", ""))
                prerequisite_topics = data.get("prerequisite_topics", [])
                needs_chunking = bool(data.get("needs_chunking", False))
                confidence = float(data.get("confidence", 0.5))

                # Clamp values
                estimated_minutes = max(5, min(120, estimated_minutes))
                difficulty = max(1, min(5, difficulty))
                confidence = max(0.0, min(1.0, confidence))
                if not isinstance(prerequisite_topics, list):
                    prerequisite_topics = []

                return {
                    "success": True,
                    "topic": topic,
                    "estimated_minutes": estimated_minutes,
                    "difficulty": difficulty,
                    "blurb": blurb,
                    "prerequisite_topics": json.dumps(prerequisite_topics),
                    "needs_chunking": needs_chunking,
                    "confidence": confidence,
                }
            except Exception as exc:
                last_error = str(exc)
                continue

        return {
            "success": False,
            "error": "classification_failed",
            "error_detail": last_error,
        }

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_classify_one, r): r for r in rows}
        done = 0

        for future in as_completed(futures):
            item = futures[future]
            item_id = item["id"]
            done += 1

            try:
                result = future.result()
            except Exception as exc:
                db.execute(
                    "UPDATE items SET status = 'error', error_reason = ? WHERE id = ?",
                    (f"unexpected_error:{str(exc)[:200]}", item_id),
                )
                db.commit()
                failed += 1
                progress_callback("Classifying", done, total)
                continue

            if result.get("success"):
                confidence = result["confidence"]
                error_reason: str | None = None
                if confidence < 0.7:
                    error_reason = "low_confidence_review"
                    low_confidence += 1
                else:
                    succeeded += 1

                chunk_total = 2 if result["needs_chunking"] else 1

                db.execute(
                    """UPDATE items
                       SET topic = ?, estimated_minutes = ?, difficulty = ?,
                           blurb = ?, prerequisite_topics = ?, chunk_total = ?,
                           error_reason = ?
                     WHERE id = ?""",
                    (
                        result["topic"],
                        result["estimated_minutes"],
                        result["difficulty"],
                        result["blurb"],
                        result["prerequisite_topics"],
                        chunk_total,
                        error_reason,
                        item_id,
                    ),
                )
            else:
                db.execute(
                    "UPDATE items SET status = 'error', error_reason = ? WHERE id = ?",
                    (result.get("error", "classification_failed"), item_id),
                )
                failed += 1

            db.commit()
            detail = f"{failed} failed" if failed else None
            progress_callback("Classifying", done, total, detail)

    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "low_confidence": low_confidence,
    }


# ---------------------------------------------------------------------------
# Stage 2: assign_prerequisites
# ---------------------------------------------------------------------------

_PREREQ_SYSTEM = (
    "You are a curriculum-design assistant. Return ONLY valid JSON. "
    "Do NOT include markdown fences or extra text."
)

_PREREQ_PROMPT_TPL = """\
You are organizing a reading list into a curriculum. Below are {count} items in the topic "{topic}". For each item, decide which other items (if any) should be read first.

Items:
{items}

Return ONLY this JSON array:
[{{"item_id": <id>, "requires_item_ids": [<ids>], "confidence": <0.0-1.0>}}, ...]

Only include items that have prerequisites. Confidence should reflect how certain you are that reading the prerequisite first is necessary (not just helpful). Be conservative — most items have zero prerequisites. A prerequisite means "you will be confused if you haven't read this first."
"""


def assign_prerequisites(
    db: sqlite3.Connection,
    model_config: dict[str, str],
    progress_callback: Callable | None = None,
) -> dict[str, int]:
    """Stage 2: Infer prerequisite relationships between items.

    Groups classified items by topic, sends batches of up to 20 items to
    the LLM, and stores edges in ``item_prerequisites``.  After all batches
    runs DFS cycle detection and breaks cycles at the lowest-confidence edge.

    Returns a dict with keys: batches, edges_added, cycles_broken.
    """
    if progress_callback is None:
        progress_callback = _default_progress

    rows = db.execute(
        "SELECT id, title, topic, blurb FROM items "
        "WHERE topic IS NOT NULL AND status = 'unread' "
        "ORDER BY topic"
    ).fetchall()

    # Group by topic
    by_topic: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        topic = r["topic"] or "other"
        by_topic.setdefault(topic, []).append(r)

    # Only process topics with > 1 item
    candidates = {t: items for t, items in by_topic.items() if len(items) > 1}
    total_batches = sum(
        (len(items) + 19) // 20 for items in candidates.values()
    )
    if total_batches == 0:
        progress_callback("Prerequisites", 0, 0)
        return {"batches": 0, "edges_added": 0, "cycles_broken": 0}

    batch_num = 0
    edges_added = 0

    for topic, items in candidates.items():
        for batch_start in range(0, len(items), 20):
            batch = items[batch_start : batch_start + 20]
            batch_num += 1

            items_text = "\n".join(
                f"ID {r['id']}: {r['title'] or 'Untitled'} — {r['blurb'] or ''}"
                for r in batch
            )

            user_prompt = _PREREQ_PROMPT_TPL.format(
                count=len(batch),
                topic=topic,
                items=items_text,
            )

            try:
                raw = call_llm(
                    model_config, _PREREQ_SYSTEM, user_prompt,
                    temperature=0.2, max_tokens=3000,
                )
                prereqs = _parse_json(raw)
                if not isinstance(prereqs, list):
                    prereqs = [prereqs]
            except Exception:
                progress_callback(
                    "Prerequisites", batch_num, total_batches,
                    f"batch failed (topic={topic})",
                )
                continue

            for entry in prereqs:
                item_id = entry.get("item_id")
                requires = entry.get("requires_item_ids", [])
                conf = entry.get("confidence", 0.5)
                if not item_id or not requires:
                    continue
                for prereq_id in requires:
                    try:
                        db.execute(
                            "INSERT OR REPLACE INTO item_prerequisites "
                            "(item_id, prereq_id, confidence) VALUES (?, ?, ?)",
                            (int(item_id), int(prereq_id), float(conf)),
                        )
                        edges_added += 1
                    except (ValueError, sqlite3.Error):
                        continue

            db.commit()
            progress_callback("Prerequisites", batch_num, total_batches)

    # -- Cycle detection ----------------------------------------------------
    cycles_broken = _break_cycles(db)
    return {
        "batches": total_batches,
        "edges_added": edges_added,
        "cycles_broken": cycles_broken,
    }


def _break_cycles(db: sqlite3.Connection) -> int:
    """Detect and break cycles in ``item_prerequisites`` via DFS.

    Repeatedly finds a cycle, removes the lowest-confidence edge, and
    continues until the graph is acyclic.

    Returns the number of edges removed.
    """
    cycles_broken = 0

    while True:
        rows = db.execute(
            "SELECT item_id, prereq_id, confidence FROM item_prerequisites"
        ).fetchall()

        adj: dict[int, list[int]] = {}
        edge_confidence: dict[tuple[int, int], float] = {}
        for r in rows:
            adj.setdefault(r["item_id"], []).append(r["prereq_id"])
            edge_confidence[(r["item_id"], r["prereq_id"])] = r["confidence"]

        if not adj:
            break

        # ----- DFS cycle detection -----
        visited: set[int] = set()
        stack: set[int] = set()
        cycle: list[int] | None = None

        def _dfs(node: int, path: list[int]) -> bool:
            nonlocal cycle
            visited.add(node)
            stack.add(node)
            for neighbor in adj.get(node, []):
                if neighbor in stack:
                    # Found a cycle: trace from neighbor to current node
                    idx = path.index(neighbor)
                    cycle = list(path[idx:]) + [neighbor]
                    return True
                if neighbor not in visited:
                    if _dfs(neighbor, path + [neighbor]):
                        return True
            stack.remove(node)
            return False

        for node in list(adj.keys()):
            if node not in visited:
                if _dfs(node, [node]):
                    break

        if not cycle:
            break  # No more cycles

        # Find the lowest-confidence edge on the cycle
        min_conf = float("inf")
        min_edge: tuple[int, int] | None = None
        for i in range(len(cycle) - 1):
            a, b = cycle[i], cycle[i + 1]
            conf = edge_confidence.get((a, b), 0.5)
            if conf < min_conf:
                min_conf = conf
                min_edge = (a, b)

        if min_edge is not None:
            db.execute(
                "DELETE FROM item_prerequisites WHERE item_id = ? AND prereq_id = ?",
                min_edge,
            )
            db.commit()
            cycles_broken += 1

    return cycles_broken


# ---------------------------------------------------------------------------
# Stage 3: chunk_items
# ---------------------------------------------------------------------------

def chunk_items(
    db: sqlite3.Connection,
    model_config: dict[str, str],
    progress_callback: Callable | None = None,
) -> dict[str, int]:
    """Stage 3: Split long items into chunked child items.

    Strategies by content type:

    * **YouTube** — ``yt-dlp`` chapter markers, fallback uniform 25-30 min.
    * **arXiv / PDF** — ``pdftotext`` section headers, LLM groups into chunks.
    * **Book** — Open Library TOC API, fallback uniform 3-5 chunks.
    * **Wikipedia** — API H2 sections.
    * **GitHub** — README + source-dir chunks.
    * **Default** — uniform ~25 minute splits.

    Each chunk becomes a row in ``items`` with ``parent_item_id`` set.
    """
    if progress_callback is None:
        progress_callback = _default_progress

    rows = db.execute(
        """SELECT id, url, title, domain, content_type, estimated_minutes,
                  topic, difficulty, blurb, prerequisite_topics, local_file_path
           FROM items
           WHERE (needs_chunking = 1 OR estimated_minutes > 60)
             AND parent_item_id IS NULL
             AND status = 'unread'"""
    ).fetchall()

    total = len(rows)
    if total == 0:
        progress_callback("Chunking", 0, 0)
        return {"total": 0, "chunked": 0, "skipped": 0}

    chunked = skipped = 0

    for idx, item in enumerate(rows, 1):
        try:
            children = _chunk_for_item(item, model_config)
            item_id = item["id"]

            if not children:
                skipped += 1
                progress_callback("Chunking", idx, total, "no chunks")
                continue

            chunk_total = len(children)
            db.execute(
                "UPDATE items SET chunk_total = ? WHERE id = ?",
                (chunk_total, item_id),
            )

            for ci, child in enumerate(children, 1):
                child_title = child.get("title", f"{item['title'] or 'Untitled'} (Part {ci})")
                child_url = child.get("url", item["url"])
                # Ensure unique URL for each chunk
                if child_url and not child.get("url"):
                    sep = "&" if "?" in child_url else "?"
                    child_url = f"{child_url}{sep}chunk={ci}"
                db.execute(
                    """INSERT OR IGNORE INTO items
                       (url, title, domain, content_type, description,
                        estimated_minutes, difficulty, topic, blurb,
                        prerequisite_topics, chunk_total, chunk_index,
                        parent_item_id, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread')""",
                    (
                        child_url,
                        child_title,
                        item["domain"],
                        item["content_type"],
                        child.get("description", item["blurb"] or ""),
                        child.get(
                            "estimated_minutes",
                            max(1, (item["estimated_minutes"] or 60) // chunk_total),
                        ),
                        item["difficulty"],
                        item["topic"],
                        child.get("blurb", item["blurb"] or ""),
                        item["prerequisite_topics"],
                        chunk_total,
                        ci,
                        item_id,
                    ),
                )

            db.commit()
            chunked += 1
            progress_callback("Chunking", idx, total, f"{chunk_total} chunks")
        except Exception as exc:
            error = f"chunking_error:{str(exc)[:200]}"
            db.execute(
                "UPDATE items SET error_reason = ? WHERE id = ?",
                (error, item["id"]),
            )
            db.commit()
            skipped += 1
            progress_callback("Chunking", idx, total, f"error: {str(exc)[:50]}")

    return {"total": total, "chunked": chunked, "skipped": skipped}


def _chunk_for_item(
    item: sqlite3.Row, model_config: dict[str, str]
) -> list[dict[str, Any]]:
    """Dispatch to the right chunking strategy based on content metadata."""
    url = item["url"] or ""
    domain = (item["domain"] or "").lower()
    content_type = (item["content_type"] or "").lower()
    local_path = item["local_file_path"]
    est_minutes = item["estimated_minutes"] or 60
    title = item["title"] or "Untitled"

    # YouTube / video
    if "youtube" in url or "youtu.be" in url or "video" in content_type:
        return _chunk_youtube(url, est_minutes)

    # arXiv / PDF
    if (
        "arxiv" in domain
        or "pdf" in content_type
        or (local_path and local_path.lower().endswith(".pdf"))
    ):
        return _chunk_pdf(local_path, url, title, est_minutes, model_config)

    # Book
    if "book" in content_type or domain in ("openlibrary.org", "books", "goodreads"):
        return _chunk_book(url, title, est_minutes)

    # Wikipedia
    if "wikipedia" in domain or "wiki" in content_type or "/wiki/" in url:
        return _chunk_wikipedia(url)

    # GitHub
    if "github" in domain or "git" in content_type:
        return _chunk_github(url)

    # Default: uniform
    return _chunk_uniform(title, est_minutes)


# -- Chunking strategies ------------------------------------------------------

def _chunk_uniform(
    title: str, estimated_minutes: int, chunk_minutes: int = 25
) -> list[dict[str, Any]]:
    """Split into uniform chunks of roughly *chunk_minutes* each."""
    num_chunks = max(1, round(estimated_minutes / chunk_minutes))
    chunk_time = max(1, estimated_minutes // num_chunks)
    return [
        {
            "title": f"{title} (Part {i + 1})",
            "estimated_minutes": chunk_time,
            "blurb": f"Part {i + 1} of {num_chunks}",
        }
        for i in range(num_chunks)
    ]


def _chunk_youtube(url: str, estimated_minutes: int) -> list[dict[str, Any]]:
    """Try ``yt-dlp`` for chapter markers; fall back to uniform chunks."""
    from json import JSONDecodeError

    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-download", url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            chapters = data.get("chapters") or []
            if chapters:
                children = []
                title = data.get("title", "Video")
                for ch in chapters:
                    start_sec = ch.get("start_time", 0)
                    end_sec = ch.get("end_time", start_sec + 60)
                    duration_min = max(1, (end_sec - start_sec) // 60)
                    ch_title = ch.get("title", f"Chapter {len(children) + 1}")
                    children.append(
                        {
                            "title": f"{title} — {ch_title}",
                            "estimated_minutes": duration_min,
                            "blurb": ch_title,
                            "url": f"{url}&t={int(start_sec)}s",
                        }
                    )
                if children:
                    return children
    except (FileNotFoundError, subprocess.TimeoutExpired, JSONDecodeError, KeyError):
        pass

    return _chunk_uniform("Video", estimated_minutes, 30)


def _chunk_pdf(
    local_path: str | None,
    url: str,
    title: str,
    estimated_minutes: int,
    model_config: dict[str, str],
) -> list[dict[str, Any]]:
    """Extract section headers via ``pdftotext`` and ask LLM for chunk boundaries."""
    headers: list[str] = []
    if local_path and os.path.isfile(local_path):
        try:
            result = subprocess.run(
                ["pdftotext", local_path, "-"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if re.match(r"^(?:[A-Z][A-Za-z\s]+|(?:Chapter|Section|\d+\.)\s)", line):
                        if 3 < len(line) < 200:
                            headers.append(line)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if not headers:
        return _chunk_uniform(title, estimated_minutes, 30)

    # Send only headers to LLM for chunking (NOT full text)
    system = "You are a document structuring assistant. Return ONLY valid JSON."
    prompt = (
        f"Below are section headers extracted from a PDF titled \"{title}\". "
        f"Group them into {max(2, estimated_minutes // 25)} chunks for a "
        f"reading curriculum.\n\n"
        f"Headers:\n" + "\n".join(headers) + "\n\n"
        f"Return ONLY this JSON array:\n"
        f'[{{"chunk": 1, "title": "<chunk title>", '
        f'"estimated_minutes": <int>}}, ...]'
    )

    try:
        raw = call_llm(model_config, system, prompt, temperature=0.2)
        chunks = _parse_json(raw)
        if isinstance(chunks, list) and chunks:
            per = max(1, estimated_minutes // len(chunks))
            return [
                {
                    "title": c.get("title", f"Part {i + 1}"),
                    "estimated_minutes": c.get("estimated_minutes", per),
                    "blurb": c.get("title", ""),
                }
                for i, c in enumerate(chunks)
            ]
    except Exception:
        pass

    return _chunk_uniform(title, estimated_minutes, 30)


def _chunk_book(url: str, title: str, estimated_minutes: int) -> list[dict[str, Any]]:
    """Try Open Library API for table of contents; fall back to uniform 3-5 chunks."""
    try:
        params = urllib.parse.urlencode({"q": title, "limit": 1})
        resp = urllib.request.urlopen(
            f"https://openlibrary.org/search.json?{params}", timeout=15
        )
        data = json.loads(resp.read().decode("utf-8"))
        docs = data.get("docs", [])
        if docs:
            key = docs[0].get("key", "")
            if key:
                resp2 = urllib.request.urlopen(
                    f"https://openlibrary.org{key}.json", timeout=15
                )
                detail = json.loads(resp2.read().decode("utf-8"))
                toc = detail.get("table_of_contents") or []
                if len(toc) >= 3:
                    per_chunk = max(1, len(toc) // max(3, estimated_minutes // 25))
                    chunks = []
                    for i in range(0, len(toc), per_chunk):
                        group = toc[i : i + per_chunk]
                        chunk_title = group[0].get("title", "") if group else ""
                        chunks.append(
                            {
                                "title": f"{title} — {chunk_title}"
                                if chunk_title
                                else f"{title} (Part {len(chunks) + 1})",
                                "estimated_minutes": max(
                                    1,
                                    estimated_minutes
                                    // ((len(toc) // per_chunk) + 1),
                                ),
                                "blurb": f"Covers sections {i + 1}–{min(i + per_chunk, len(toc))}",
                            }
                        )
                    if len(chunks) >= 2:
                        return chunks
    except Exception:
        pass

    # Fallback: uniform 3-5 chunks
    num_chunks = min(5, max(3, round(estimated_minutes / 30)))
    chunk_time = max(1, estimated_minutes // num_chunks)
    return [
        {
            "title": f"{title} (Part {i + 1})",
            "estimated_minutes": chunk_time,
            "blurb": f"Part {i + 1} of {num_chunks}",
        }
        for i in range(num_chunks)
    ]


def _chunk_wikipedia(url: str) -> list[dict[str, Any]]:
    """Try Wikipedia API for H2-level sections; fall back to uniform chunks."""
    match = re.search(r"/wiki/([^#?]+)", url)
    if not match:
        return _chunk_uniform("Wikipedia article", 30, 25)

    page_title = urllib.parse.unquote(match.group(1))
    try:
        api_url = (
            "https://en.wikipedia.org/w/api.php"
            f"?action=parse&page={urllib.parse.quote(page_title)}"
            "&prop=sections&format=json"
        )
        resp = urllib.request.urlopen(api_url, timeout=15)
        data = json.loads(resp.read().decode("utf-8"))

        sections = data.get("parse", {}).get("sections", [])
        h2 = [s for s in sections if s.get("level") == "2"]

        if h2:
            children = []
            for sec in h2:
                sec_title = sec.get("line", "")
                anchor = sec.get("anchor", "")
                section_url = (
                    f"{url}#{urllib.parse.quote(anchor)}" if anchor else url
                )
                children.append(
                    {
                        "title": f"{page_title.replace('_', ' ')} — {sec_title}",
                        "estimated_minutes": 10,
                        "blurb": sec_title,
                        "url": section_url,
                    }
                )
            if children:
                return children
    except Exception:
        pass

    return _chunk_uniform(page_title.replace("_", " "), 30, 25)


def _chunk_github(url: str) -> list[dict[str, Any]]:
    """Chunk a GitHub repo: README, source, and tests/documentation."""
    match = re.search(r"github\.com/([^/]+/[^/]+)", url)
    repo = match.group(1).rstrip("/") if match else "repository"

    return [
        {
            "title": f"{repo} — README",
            "estimated_minutes": 15,
            "blurb": "Project overview and getting started",
            "url": f"https://github.com/{repo}#readme",
        },
        {
            "title": f"{repo} — Source Code",
            "estimated_minutes": 20,
            "blurb": "Main source directories and implementation",
            "url": f"https://github.com/{repo}/tree/main",
        },
        {
            "title": f"{repo} — Tests & Docs",
            "estimated_minutes": 10,
            "blurb": "Tests, documentation, and examples",
            "url": f"https://github.com/{repo}/tree/main/tests",
        },
    ]


# ---------------------------------------------------------------------------
# Stage 4: pre_generate_lectures
# ---------------------------------------------------------------------------

_LECTURE_SYSTEM = (
    "You are a curriculum designer preparing lecture materials. "
    "Return ONLY valid JSON. Do NOT include markdown fences or extra text."
)

_LECTURE_PROMPT_TPL = """\
You are preparing a lecture for a personal learning curriculum. Given the item metadata below, generate:
1. A warmup paragraph (2-3 sentences) that explains why this topic matters and what to look for. Frame as context, not summary — use phrases like "Based on the abstract..." or "This appears to address..."
2. Three guided questions that help the reader engage actively. Questions should be observational ("What dataset...?", "How does X relate to Y?") not inferential ("Why is this correct?").

Metadata: title={title}, description={description}, domain={domain}, topic={topic}, difficulty={difficulty}/5

Return ONLY this JSON:
{{
  "warmup": "<2-3 sentences, framed as context not summary>",
  "guided_questions": ["<observational question 1>", "<observational question 2>", "<observational question 3>"]
}}"""

_FALLBACK_WARMUP_TPL = "Today's session: {title}. Read/watch and note what you learn."

_FALLBACK_QUESTIONS = json.dumps(
    [
        "What is the main argument or finding?",
        "What evidence or examples support it?",
        "How does this connect to what you already know?",
    ]
)


def pre_generate_lectures(
    db: sqlite3.Connection,
    model_config: dict[str, str],
    progress_callback: Callable | None = None,
) -> dict[str, int]:
    """Stage 4: Generate warmup text and guided questions for each item.

    Skips items that already have a warmup set.  Falls back to a generic
    template if the LLM call fails.

    Returns a dict with keys: total, generated, fallbacks.
    """
    if progress_callback is None:
        progress_callback = _default_progress

    rows = db.execute(
        "SELECT id, title, description, domain, topic, difficulty, url "
        "FROM items "
        "WHERE warmup IS NULL AND status = 'unread'"
    ).fetchall()

    total = len(rows)
    if total == 0:
        progress_callback("Lectures", 0, 0)
        return {"total": 0, "generated": 0, "fallbacks": 0}

    generated = fallbacks = 0

    def _generate_one(item: sqlite3.Row) -> dict[str, Any]:
        title = item["title"] or ""
        user_prompt = _LECTURE_PROMPT_TPL.format(
            title=title,
            description=item["description"] or "",
            domain=item["domain"] or "",
            topic=item["topic"] or "other",
            difficulty=item["difficulty"] or 3,
        )

        try:
            raw = call_llm(
                model_config, _LECTURE_SYSTEM, user_prompt, temperature=0.4
            )
            data = _parse_json(raw)

            warmup = str(
                data.get("warmup", _FALLBACK_WARMUP_TPL.format(title=title))
            )
            questions = data.get("guided_questions", [])
            if not isinstance(questions, list) or len(questions) == 0:
                questions = json.loads(_FALLBACK_QUESTIONS)

            return {
                "success": True,
                "warmup": warmup,
                "guided_questions": json.dumps(questions[:3]),
                "was_fallback": False,
            }
        except Exception:
            return {
                "success": True,
                "warmup": _FALLBACK_WARMUP_TPL.format(title=title),
                "guided_questions": _FALLBACK_QUESTIONS,
                "was_fallback": True,
            }

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_generate_one, r): r for r in rows}
        done = 0

        for future in as_completed(futures):
            item = futures[future]
            done += 1

            try:
                result = future.result()
            except Exception:
                db.execute(
                    "UPDATE items SET warmup = ?, guided_questions = ? WHERE id = ?",
                    (
                        _FALLBACK_WARMUP_TPL.format(
                            title=item["title"] or "Untitled"
                        ),
                        _FALLBACK_QUESTIONS,
                        item["id"],
                    ),
                )
                db.commit()
                fallbacks += 1
                progress_callback("Lectures", done, total)
                continue

            db.execute(
                "UPDATE items SET warmup = ?, guided_questions = ? WHERE id = ?",
                (result["warmup"], result["guided_questions"], item["id"]),
            )
            db.commit()

            if result.get("was_fallback"):
                fallbacks += 1
            else:
                generated += 1

            progress_callback("Lectures", done, total)

    return {"total": total, "generated": generated, "fallbacks": fallbacks}


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

def run_pipeline(
    db: sqlite3.Connection,
    config: dict[str, str],
    progress_callback: Callable | None = None,
) -> dict[str, dict[str, int]]:
    """Execute all four pipeline stages in order and return per-stage stats."""
    if progress_callback is None:
        progress_callback = _default_progress

    print("Stage 1/4: Classification")
    t0 = time.time()
    stats1 = classify_items(db, config, progress_callback)
    t1 = time.time()
    _summarise("Classify items", stats1, t1 - t0)

    print("\nStage 2/4: Prerequisites")
    t0 = time.time()
    stats2 = assign_prerequisites(db, config, progress_callback)
    t1 = time.time()
    _summarise("Assign prerequisites", stats2, t1 - t0)

    print("\nStage 3/4: Chunking")
    t0 = time.time()
    stats3 = chunk_items(db, config, progress_callback)
    t1 = time.time()
    _summarise("Chunk items", stats3, t1 - t0)

    print("\nStage 4/4: Lecture pre-generation")
    t0 = time.time()
    stats4 = pre_generate_lectures(db, config, progress_callback)
    t1 = time.time()
    _summarise("Pre-generate lectures", stats4, t1 - t0)

    return {"classify": stats1, "prerequisites": stats2, "chunk": stats3, "lectures": stats4}


def _summarise(name: str, stats: dict[str, int], elapsed: float) -> None:
    """Print a compact one-line summary for a pipeline stage."""
    items = stats.get("total", 0)
    # Pick the "good" count from whatever the stage reports
    ok = (
        stats.get("succeeded")
        or stats.get("chunked")
        or stats.get("generated")
        or items
    )
    fails = stats.get("failed", 0) or stats.get("skipped", 0)
    low = stats.get("low_confidence", 0)
    parts = [f"└─ {name}: {items} items, {elapsed:.1f}s, {ok} ok"]
    if low:
        parts.append(f", {low} low-confidence")
    if fails:
        parts.append(f", {fails} errors")
    print("".join(parts))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point: parse args, init DB, run pipeline, print summary."""
    # ── Import sibling modules (works when run as ``python classifier.py``) ──
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from models import init_db, get_db
    from config import get_llm_config

    print("=" * 60)
    print("  Lecture Today — Classification Pipeline")
    print("=" * 60)

    # Initialise database
    init_db()

    # Read LLM configuration
    config = get_llm_config()
    if not config["api_key"]:
        print("\nError: Set ANTHROPIC_API_KEY or LLM_API_KEY in your .env file.")
        sys.exit(1)

    print(f"  Model:    {config['model']}")
    print(f"  Base URL: {config['base_url']}")
    print()

    db = get_db()
    stats = run_pipeline(db, config)

    # -- Overall summary -------------------------------------------------------
    _print_overall_summary(stats, config)

    db.close()


def _print_overall_summary(
    stats: dict[str, dict[str, int]], config: dict[str, str]
) -> None:
    """Print a final summary table and record the run."""
    from models import get_db

    s1 = stats["classify"]
    s2 = stats["prerequisites"]
    s3 = stats["chunk"]
    s4 = stats["lectures"]

    print()
    print("=" * 60)
    print("  Pipeline complete")
    print("=" * 60)

    if s1.get("total"):
        print(f"  Items classified:             {s1['total']}")
    if s1.get("succeeded"):
        print(f"  Successfully classified:      {s1['succeeded']}")
    if s1.get("low_confidence"):
        print(f"  Low confidence (flagged):     {s1['low_confidence']}")
    if s1.get("failed"):
        print(f"  Classification errors:        {s1['failed']}")

    if s2.get("batches"):
        print(f"  Prerequisite batches:         {s2['batches']}")
    if s2.get("edges_added"):
        print(f"  Prerequisite edges added:     {s2['edges_added']}")
    if s2.get("cycles_broken"):
        print(f"  Cycles broken:                {s2['cycles_broken']}")

    if s3.get("chunked"):
        print(f"  Items chunked:                {s3['chunked']}")
    if s3.get("skipped"):
        print(f"  Chunking skips/errors:        {s3['skipped']}")

    if s4.get("generated"):
        print(f"  Lectures pre-generated:       {s4['generated']}")
    if s4.get("fallbacks"):
        print(f"  Lecture fallbacks:            {s4['fallbacks']}")

    # Record run in classification_runs
    db2 = get_db()
    db2.execute(
        "INSERT INTO classification_runs (items_processed, model_used) VALUES (?, ?)",
        (s1.get("total", 0), config["model"]),
    )
    db2.commit()
    db2.close()


if __name__ == "__main__":
    main()
