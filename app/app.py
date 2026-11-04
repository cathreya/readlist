"""Flask web app — the Lecture Today server."""

import os
import sys
import json
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory

sys.path.insert(0, os.path.dirname(__file__))

from models import init_db, get_db
from config import get_llm_config

app = Flask(__name__)

_db_initialized = False

def _ensure_db():
    global _db_initialized
    if not _db_initialized:
        init_db()
        _db_initialized = True

def get_curriculum():
    from curriculum import pick_next_item, pick_lighter_item
    return pick_next_item, pick_lighter_item

def get_lecture_gen():
    from lecture_generator import build_lecture_response, personalize
    return build_lecture_response, personalize

def get_anki():
    from anki import generate_cards, queue_cards, flush_card_queue
    return generate_cards, queue_cards, flush_card_queue


def _chunk_uniform(title: str, estimated_minutes: int, chunk_minutes: int = 25) -> list[dict]:
    """Split into uniform chunks of roughly chunk_minutes each."""
    num_chunks = max(2, min(4, round(estimated_minutes / chunk_minutes)))
    chunk_time = max(1, estimated_minutes // num_chunks)
    return [
        {
            "title": f"{title} (Part {i + 1})",
            "estimated_minutes": chunk_time,
            "blurb": f"Part {i + 1} of {num_chunks}",
        }
        for i in range(num_chunks)
    ]


def _trigger_chunking(db, item_id: int) -> int | None:
    """Chunk an item marked as too_long and return the first chunk's item id."""
    item = db.execute(
        "SELECT * FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if not item:
        return None

    title = item["title"] or "Untitled"
    est = item["estimated_minutes"] or 60
    children = _chunk_uniform(title, est)
    chunk_total = len(children)

    db.execute("UPDATE items SET chunk_total = ? WHERE id = ?", (chunk_total, item_id))

    first_chunk_id = None
    for ci, child in enumerate(children, 1):
        child_url = item["url"]
        if child_url:
            # Make each chunk URL unique to satisfy the UNIQUE constraint
            sep = "&" if "?" in child_url else "?"
            child_url = f"{child_url}{sep}chunk={ci}"
        # For YouTube, override with timestamp
        if "youtube" in (item["domain"] or "") and "&t=" not in (item["url"] or ""):
            start_sec = (ci - 1) * est * 60 // chunk_total
            child_url = f"{item['url']}&t={start_sec}s" if item["url"] else None

        cur = db.execute(
            """INSERT OR IGNORE INTO items
               (url, title, domain, content_type, description,
                estimated_minutes, difficulty, topic, blurb,
                chunk_total, chunk_index, parent_item_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unread')""",
            (
                child_url,
                child["title"],
                item["domain"],
                item["content_type"],
                child["blurb"],
                child["estimated_minutes"],
                item["difficulty"],
                item["topic"],
                child["blurb"],
                chunk_total,
                ci,
                item_id,
            ),
        )
        if first_chunk_id is None:
            first_chunk_id = cur.lastrowid

    db.commit()
    return first_chunk_id


@app.before_request
def before_request():
    if not request.path.startswith("/static"):
        _ensure_db()


@app.route("/")
def index():
    db = get_db()
    row = db.execute("SELECT COUNT(*) as c FROM items WHERE status != 'dead'").fetchone()
    if row["c"] == 0:
        return redirect(url_for("setup"))

    # Check for in-progress item from a crash/restart
    in_progress = db.execute(
        "SELECT id, title FROM items WHERE status = 'in_progress' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()

    if in_progress:
        item = db.execute("SELECT * FROM items WHERE id = ?", (in_progress["id"],)).fetchone()
        build_lecture, _ = get_lecture_gen()
        lecture = build_lecture(dict(item), db)
        return render_template("lecture.html", lecture=lecture, resuming=True, lighter_available=False)

    # Normal: pick next item
    pick_next, pick_lighter = get_curriculum()
    result = pick_next(db)

    if result.get("message") == "celebration":
        return render_template("celebration.html")

    item = result["item"]
    if item is None:
        # If there are unclassified items, nudge the user to run the pipeline
        unclassified = db.execute(
            "SELECT COUNT(*) as c FROM items WHERE difficulty IS NULL AND status != 'dead'"
        ).fetchone()["c"]
        if unclassified > 0:
            return render_template(
                "celebration.html",
                message=f"{unclassified} items imported but not yet classified.",
                show_classifier_link=True
            )
        return render_template("celebration.html")

    # Mark in progress
    db.execute("UPDATE items SET status = 'in_progress' WHERE id = ?", (item["id"],))
    db.commit()

    build_lecture, _ = get_lecture_gen()
    lecture = build_lecture(dict(item), db)

    return render_template(
        "lecture.html",
        lecture=lecture,
        resuming=False,
        continue_prompt=result.get("continue_prompt"),
        lighter_available=result.get("lighter_available", True)
    )


@app.route("/setup")
def setup():
    db = get_db()
    row = db.execute("SELECT COUNT(*) as c FROM items").fetchone()
    existing_count = row["c"]
    return render_template("setup.html", existing_count=existing_count)


@app.route("/lighter", methods=["POST"])
def lighter():
    db = get_db()
    _, pick_lighter = get_curriculum()
    result = pick_lighter(db)

    if result.get("item") is None:
        from curriculum import pick_next_item
        result = pick_next_item(db)
        item = result["item"]
    else:
        item = result["item"]

    if item is None:
        return render_template("celebration.html")

    db.execute("UPDATE items SET status = 'in_progress' WHERE id = ?", (item["id"],))
    db.commit()

    build_lecture, _ = get_lecture_gen()
    lecture = build_lecture(dict(item), db)

    return render_template("lecture.html", lecture=lecture, resuming=False, lighter_available=False)


@app.route("/complete", methods=["POST"])
def complete():
    db = get_db()
    data = request.get_json()
    item_id = data.get("item_id")
    exit_note = data.get("exit_note", "")

    db.execute(
        "INSERT INTO sessions (item_id, lecture_title, warmup, guided_questions, completed_at, exit_note) "
        "SELECT ?, title, warmup, guided_questions, datetime('now'), ? FROM items WHERE id = ?",
        (item_id, exit_note, item_id)
    )
    session_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("UPDATE items SET status = 'completed' WHERE id = ?", (item_id,))
    db.commit()

    # Generate and queue Anki cards
    generate_cards, queue_cards_fn, flush_fn = get_anki()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    try:
        cards = generate_cards(dict(item), session_id, exit_note)
        queue_cards_fn(db, item_id, session_id, cards)
        flush_fn(db)
    except Exception:
        pass  # Card generation failure should not block completion

    # Get next lecture
    from curriculum import pick_next_item
    result = pick_next_item(db, prev_session_id=session_id)

    if result.get("message") == "celebration" or result.get("item") is None:
        return jsonify({"next": "celebration"})

    next_item = result["item"]
    db.execute("UPDATE items SET status = 'in_progress' WHERE id = ?", (next_item["id"],))
    db.commit()

    build_lecture, _ = get_lecture_gen()
    lecture = build_lecture(dict(next_item), db)

    return jsonify({"next": "lecture", "lecture": lecture, "continue_prompt": result.get("continue_prompt")})


@app.route("/skip", methods=["POST"])
def skip():
    db = get_db()
    data = request.get_json()
    item_id = data.get("item_id")
    reason = data.get("reason", "not_right_now")

    from models import compute_resurface_date

    # too_long is not a real skip — it's a chunking request, no cooldown
    if reason == "too_long":
        first_chunk_id = _trigger_chunking(db, item_id)
        if first_chunk_id:
            db.execute(
                "UPDATE items SET status = 'completed' WHERE id = ?",
                (item_id,)
            )
            db.execute(
                "UPDATE items SET status = 'in_progress' WHERE id = ?",
                (first_chunk_id,)
            )
            db.commit()
            build_lecture, _ = get_lecture_gen()
            next_item = db.execute("SELECT * FROM items WHERE id = ?", (first_chunk_id,)).fetchone()
            lecture = build_lecture(dict(next_item), db)
            return jsonify({"next": "lecture", "lecture": lecture, "continue_prompt": None})
        else:
            db.execute(
                "UPDATE items SET status = 'unread', error_reason = 'needs_chunking', needs_chunking = 1 WHERE id = ?",
                (item_id,)
            )
    else:
        # Check graveyard before inserting skip
        if reason == "not_interested":
            skip_count = db.execute(
                "SELECT COUNT(*) as c FROM skips WHERE item_id = ? AND reason = 'not_interested'",
                (item_id,)
            ).fetchone()["c"]
            if skip_count >= 2:  # This will be the 3rd skip
                resurface = None  # Permanent graveyard
            else:
                resurface = compute_resurface_date(reason)
        else:
            resurface = compute_resurface_date(reason)

        db.execute(
            "INSERT INTO skips (item_id, reason, resurface_after) VALUES (?, ?, ?)",
            (item_id, reason, resurface)
        )

        if reason == "not_interested":
            skip_count = db.execute(
                "SELECT COUNT(*) as c FROM skips WHERE item_id = ? AND reason = 'not_interested'",
                (item_id,)
            ).fetchone()["c"]
            if skip_count >= 3:
                db.execute("UPDATE items SET status = 'skipped', error_reason = 'graveyard' WHERE id = ?", (item_id,))
            else:
                db.execute("UPDATE items SET status = 'skipped' WHERE id = ?", (item_id,))
        else:
            db.execute("UPDATE items SET status = 'skipped' WHERE id = ?", (item_id,))

    db.commit()

    # Get next lecture
    from curriculum import pick_next_item
    result = pick_next_item(db)

    if result.get("message") == "celebration" or result.get("item") is None:
        return jsonify({"next": "celebration"})

    next_item = result["item"]
    db.execute("UPDATE items SET status = 'in_progress' WHERE id = ?", (next_item["id"],))
    db.commit()

    build_lecture, _ = get_lecture_gen()
    lecture = build_lecture(dict(next_item), db)

    return jsonify({"next": "lecture", "lecture": lecture, "continue_prompt": result.get("continue_prompt")})


@app.route("/setup", methods=["POST"])
def setup_post():
    data = request.get_json()
    messages_path = data.get("messages_path", "../messages.html")
    files_path = data.get("files_path", "../files")

    # Resolve paths safely — reject absolute paths and path traversal
    import os
    app_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(app_dir)

    def safe_resolve(user_path, default):
        if not user_path or user_path != user_path.strip():
            return os.path.join(project_root, default)
        # Reject absolute paths and traversal attempts
        if os.path.isabs(user_path) or ".." in user_path:
            return os.path.join(project_root, default)
        return os.path.normpath(os.path.join(app_dir, user_path))

    messages_path = safe_resolve(messages_path, "messages.html")
    files_path = safe_resolve(files_path, "files")

    # Parse messages.html
    from parser import parse_messages_html
    items = parse_messages_html(messages_path, files_path)

    # Fetch metadata for all parsed items
    from metadata import fetch_all
    items = fetch_all(items)

    db = get_db()
    inserted = 0
    notes_inserted = 0
    dead_count = 0
    for item in items:
        try:
            # Notes and file-only items have no URL — use the content_type or a fallback
            url = item.get("url")
            if not url:
                url = f"note://{item.get('content_type', 'unknown')}/{hash(item.get('description', ''))}"

            status = "unread"
            error_reason = None
            if item.get("fetch_error") in ("not_found", "gone", "server_error"):
                status = "dead"
                error_reason = item["fetch_error"]
                dead_count += 1

            db.execute(
                "INSERT INTO items (url, title, domain, content_type, description, local_file_path, status, error_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (url, item.get("title", ""), item.get("domain", ""),
                 item.get("content_type", "unknown"), item.get("description", ""),
                 item.get("local_file_path"), status, error_reason)
            )
            inserted += 1
            if item.get("content_type") == "note":
                notes_inserted += 1
        except sqlite3.IntegrityError:
            pass  # Duplicate URL, skip

    db.commit()

    return jsonify({
        "redirect": "/",
        "items_found": len(items),
        "items_inserted": inserted,
        "notes_saved": notes_inserted,
        "dead_links": dead_count,
        "next_step": "Run: python classifier.py"
    })


@app.route("/add", methods=["POST"])
def add_url():
    """Manually add a single URL to the queue."""
    db = get_db()
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    from metadata import fetch_metadata
    from parser import normalize_domain, detect_content_type

    domain = normalize_domain(url)
    content_type = detect_content_type(url, domain)
    meta = fetch_metadata(url, domain, content_type)

    status = "unread"
    error_reason = None
    if meta.get("error") in ("not_found", "gone", "server_error"):
        status = "dead"
        error_reason = meta["error"]

    try:
        db.execute(
            "INSERT INTO items (url, title, domain, content_type, description, status, error_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (url, meta.get("title", ""), domain, content_type,
             meta.get("description", ""), status, error_reason)
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "URL already exists"}), 409

    return jsonify({
        "success": True,
        "item_id": db.execute("SELECT last_insert_rowid()").fetchone()[0],
        "status": status,
    })


@app.route("/graveyard")
def graveyard():
    """Browse graveyard and error items."""
    db = get_db()
    graveyard_items = db.execute(
        "SELECT id, url, title, domain, content_type, topic, error_reason, created_at "
        "FROM items WHERE error_reason = 'graveyard' OR status = 'dead' OR status = 'error' "
        "ORDER BY created_at DESC"
    ).fetchall()
    return render_template("graveyard.html", items=[dict(r) for r in graveyard_items])


@app.route("/revive", methods=["POST"])
def revive():
    """Revive a dead or error item back to the unread queue."""
    db = get_db()
    data = request.get_json()
    item_id = data.get("item_id")
    db.execute(
        "UPDATE items SET status = 'unread', error_reason = NULL WHERE id = ?",
        (item_id,)
    )
    db.commit()
    return jsonify({"success": True})


@app.route("/card-queue-status")
def card_queue_status():
    db = get_db()
    count = db.execute("SELECT COUNT(*) as c FROM card_queue WHERE flushed = 0").fetchone()["c"]
    return jsonify({"pending": count})


@app.route("/files/<path:filepath>")
def serve_file(filepath):
    """Serve local files (PDFs, etc.) from the project's files/ directory."""
    import os
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    safe_path = os.path.normpath(os.path.join(project_root, "files", filepath))
    if not safe_path.startswith(os.path.join(project_root, "files")):
        return "Forbidden", 403
    if not os.path.isfile(safe_path):
        return "File not found", 404
    return send_from_directory(os.path.join(project_root, "files"), filepath)


if __name__ == "__main__":
    config = get_llm_config()
    if not config["api_key"]:
        print("Warning: No LLM API key set. Classification pipeline will not work.")
        print("Set ANTHROPIC_API_KEY or LLM_API_KEY in .env")
    print("Starting Lecture Today on http://localhost:8080")
    app.run(host="127.0.0.1", port=8080, debug=True)
