"""Flashcap local server.

Run from the project root::

    python -m capture.server

The server binds to 127.0.0.1 only. It has no accounts and no remote access;
the userscript talks to it over localhost.
"""

from __future__ import annotations

import html
import os
import threading
from datetime import date
from urllib.parse import urlparse

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
)

from . import __version__, library, obsidian, quiz, store, telegram
from .config import (
    get_db_path,
    get_host,
    get_llm_config,
    get_port,
    get_public_url,
    get_telegram_config,
)
from .llm import (
    LLMError,
    explain_passage,
    generate_question,
    grade_answer,
    suggest_metadata,
)
from .taxonomy import UI_CATEGORIES

from werkzeug.serving import WSGIRequestHandler


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["DB_PATH"] = db_path or get_db_path()
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
    app.json.sort_keys = False

    store.init_db(app.config["DB_PATH"])
    app.teardown_appcontext(store.close_db)

    # ------------------------------------------------------------------
    # CORS: only localhost origins. The userscript uses Greasemonkey's
    # privileged request API, so it does not need CORS at all; this exists
    # for the same-origin dashboard and local bookmarklet fallbacks.
    # ------------------------------------------------------------------
    @app.after_request
    def _cors_and_headers(response: Response) -> Response:
        origin = request.headers.get("Origin", "")
        if origin and _is_local_origin(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, POST, PATCH, DELETE, OPTIONS"
            )
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers.add("Vary", "Origin")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path.startswith("/api/") or request.path.endswith(".user.js"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.before_request
    def _check_host():
        """Reject unknown Host headers.

        The local server is reachable from any page in the browser on
        127.0.0.1; without this, a malicious site can DNS-rebind its own
        hostname to loopback and talk to the API same-origin.
        """
        if request.path.startswith("/static/"):
            return None
        if _request_host() not in _allowed_hosts():
            return jsonify({"error": "Unexpected Host header"}), 403
        return None

    @app.before_request
    def _preflight():
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            response = app.make_response(("", 204))
            return response
        return None

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    @app.get("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/quick")
    def quick():
        return render_template("quick.html")

    @app.get("/practice")
    def practice():
        return render_template("practice.html")

    @app.get("/library")
    def library_page():
        return render_template("library.html")

    # ------------------------------------------------------------------
    # Capture API
    # ------------------------------------------------------------------
    @app.post("/api/capture")
    def capture():
        """Save a manually written card. This endpoint never calls the LLM."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400

        front = _text(data.get("front"), 2000)
        back = _text(data.get("back"), 4000)
        if not front or not back:
            return jsonify({"error": "Front and back are required"}), 400

        payload = _capture_payload(data)
        try:
            card = store.insert_card(
                {
                    "front": front,
                    "back": back,
                    "category": _text(data.get("category"), 80) or "other",
                    "tags": data.get("tags"),
                    "source_url": payload["url"],
                    "source_title": payload["page_title"],
                    "source_site": payload["site"],
                    "source_author": payload["author"],
                    "selection": payload["selection"],
                    "context": payload["context"],
                    "quote": payload["selection"] or payload["focus"] or None,
                    "hint": payload["hint"],
                    "confidence": None,
                    "model": "",
                    "generated_by": "manual",
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify({"card": card}), 201

    @app.post("/api/suggest")
    def suggest():
        """Suggest category/tags only; card text remains entirely manual."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400

        payload = _capture_payload(data)
        if not (payload["selection"] or payload["focus"] or payload["context"]):
            return jsonify(
                {"error": "Nothing to classify — select some text or pass a passage"}
            ), 400

        try:
            result = suggest_metadata(
                payload,
                known_tags=store.tag_vocabulary(limit=40),
            )
        except LLMError as exc:
            return jsonify({"error": str(exc)}), 502

        return jsonify(result)

    @app.post("/api/explain")
    def explain():
        """Explain a passage inline. Nothing is stored; the popup decides."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400

        payload = _capture_payload(data)
        if not (payload["selection"] or payload["focus"] or payload["context"]):
            return jsonify(
                {"error": "Nothing to explain — highlight a passage first"}
            ), 400

        try:
            result = explain_passage(payload)
        except LLMError as exc:
            return jsonify({"error": str(exc)}), 502
        return jsonify(result)

    @app.get("/api/cards")
    def cards_index():
        cards = store.list_cards(
            query=request.args.get("q", ""),
            category=request.args.get("category", ""),
            tag=request.args.get("tag", ""),
            limit=_int_arg("limit", 1000),
        )
        return jsonify({"cards": cards, "count": len(cards)})

    @app.get("/api/cards/<int:card_id>")
    def cards_show(card_id: int):
        card = store.get_card(card_id)
        if card is None:
            return jsonify({"error": "Card not found"}), 404
        return jsonify({"card": card})

    @app.patch("/api/cards/<int:card_id>")
    def cards_update(card_id: int):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400
        fields = {key: data[key] for key in store.EDITABLE_FIELDS if key in data}
        try:
            card = store.update_card(card_id, fields)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if card is None:
            return jsonify({"error": "Card not found"}), 404
        return jsonify({"card": card})

    @app.delete("/api/cards/<int:card_id>")
    def cards_delete(card_id: int):
        if not store.delete_card(card_id):
            return jsonify({"error": "Card not found"}), 404
        return jsonify({"deleted": True, "id": card_id})

    @app.get("/api/meta")
    def meta():
        data = store.metadata()
        config = get_llm_config()
        data["category_choices"] = UI_CATEGORIES
        data["llm"] = {
            "configured": bool(config["api_key"]),
            "model": config["model"],
            "provider": config["base_url"],
        }
        return jsonify(data)

    @app.get("/api/health")
    def health():
        config = get_llm_config()
        telegram_config = get_telegram_config()
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "cards": store.metadata()["total"],
                "llm_configured": bool(config["api_key"]),
                "model": config["model"],
                "telegram": {
                    "enabled": bool(telegram_config["enabled"]),
                    "offset": store.get_state("telegram_offset", "0"),
                },
            }
        )

    # ------------------------------------------------------------------
    # Reading library
    # ------------------------------------------------------------------
    @app.get("/api/library")
    def library_index():
        query = request.args.get("q", "")
        status = request.args.get("status", "")
        site = request.args.get("site", "")
        limit = _int_arg("limit", 200)
        offset = max(0, _int_arg("offset", 0))

        items = store.list_reading_items(
            query=query,
            status=status,
            site=site,
            limit=limit,
            offset=offset,
        )
        total = store.count_reading_items(query, status, site)
        counts = store.reading_counts()
        return jsonify(
            {
                "items": items,
                "count": len(items),
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": offset + len(items) < total,
                "counts": counts,
                "sites": store.reading_sites(),
            }
        )

    @app.post("/api/library")
    def library_add():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400

        key = library.canonical_key(data.get("url"))
        if not key:
            return jsonify({"error": "Enter a valid http(s) link"}), 400

        item = store.upsert_reading_item(
            {
                "url": key,
                "url_key": key,
                "title": _text(data.get("title"), 400),
                "site": library.host_of(key),
                "source": library.SOURCE_MANUAL,
            }
        )
        return jsonify({"item": item}), 201

    @app.patch("/api/library/<int:item_id>")
    def library_update(item_id: int):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400

        previous = store.get_reading_item(item_id)
        if previous is None:
            return jsonify({"error": "Item not found"}), 404

        fields = {key: data[key] for key in ("status", "title") if key in data}
        try:
            item = store.update_reading_item(item_id, fields)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if item is None:
            return jsonify({"error": "Item not found"}), 404

        archive = None
        if item["status"] == "done" and previous["status"] != "done":
            try:
                archive = telegram.archive_done(item)
            except telegram.TelegramError as exc:
                store.update_reading_item(item_id, {"status": previous["status"]})
                return jsonify({"error": f"Could not move it to the done chat: {exc}"}), 502
            item = store.get_reading_item(item_id)

        return jsonify({"item": item, "archive": archive})

    @app.delete("/api/library/<int:item_id>")
    def library_delete(item_id: int):
        if not store.delete_reading_item(item_id):
            return jsonify({"error": "Item not found"}), 404
        return jsonify({"deleted": True, "id": item_id})

    @app.post("/api/library/<int:item_id>/undo")
    def library_undo(item_id: int):
        item = store.get_reading_item(item_id)
        if item is None:
            return jsonify({"error": "Item not found"}), 404
        if item["status"] != "done":
            return jsonify({"error": "Only a done item can be undone"}), 409

        undo = {"ok": True, "skipped": "not archived"}
        if item.get("archived_at"):
            try:
                undo = telegram.undo_done(item)
            except telegram.TelegramError as exc:
                return jsonify({"error": f"Could not undo: {exc}"}), 502

        item = store.reset_reading_archive(item_id, undo.get("message_id"))
        return jsonify({"item": item, "undo": undo})

    # Quiz me (scoped to what the reader has read)
    # ------------------------------------------------------------------
    @app.post("/api/quiz/start")
    def quiz_start():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400

        url = _text(data.get("url"), 2000)
        if urlparse(url).scheme not in ("http", "https"):
            return jsonify({"error": "A valid article URL is required"}), 400

        units = quiz.normalize_units(data.get("units"))
        if not units:
            return jsonify(
                {
                    "error": (
                        "Read a little further before quizzing. No passage above "
                        "your position is long enough yet."
                    )
                }
            ), 422

        title = _text(data.get("page_title"), 400)
        coverage = store.quiz_coverage(url)
        unit = quiz.select_unit(units, [], coverage, 0)
        if unit is None:
            return jsonify({"error": "Nothing to quiz yet"}), 422

        try:
            generated = generate_question(title, unit["heading"], unit["text"])
        except LLMError as exc:
            return jsonify({"error": str(exc)}), 502

        session = store.create_quiz_session(
            url, title, units, _int_value(data.get("total_units"), len(units))
        )
        question = quiz.build_question(unit, generated)
        store.record_quiz_coverage(url, unit["key"])
        session = (
            store.update_quiz_session(session["id"], {"question": question}) or session
        )
        return jsonify(
            {
                "session": quiz.session_view(session, coverage),
                "question": quiz.question_view(question),
            }
        ), 201

    @app.post("/api/quiz/<int:session_id>/answer")
    def quiz_answer(session_id: int):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400

        session = store.get_quiz_session(session_id)
        if session is None:
            return jsonify({"error": "Quiz session not found"}), 404

        question = session.get("question")
        if not question:
            return jsonify({"error": "This session has no open question"}), 409

        answer = _text(data.get("answer"), quiz.MAX_ANSWER_CHARS)
        if not answer:
            return jsonify({"error": "Write an answer first"}), 400

        unit = _quiz_unit(session, question.get("unit_key"))
        if unit is None:
            return jsonify({"error": "The question's passage is missing"}), 409

        try:
            grade = grade_answer(
                question.get("text", ""),
                question.get("expected") or [],
                unit["text"],
                answer,
                revealed=bool(question.get("revealed")),
                source_title=session.get("source_title") or "",
            )
        except LLMError as exc:
            # The attempt is not consumed: the same question stays open.
            return jsonify({"error": str(exc)}), 502

        updated, reply = quiz.apply_grade(question, grade)
        payload: dict = {
            "verdict": reply["verdict"],
            "feedback": reply["feedback"],
            "hint": reply["hint"],
            "revealed": reply["revealed"],
        }
        if reply.get("answer"):
            payload["answer"] = reply["answer"]

        if reply["verdict"] == "satisfactory":
            store.record_quiz_coverage(
                session["source_url"], question["unit_key"], "satisfactory"
            )
            session = store.update_quiz_session(
                session_id,
                {
                    "question": None,
                    "asked": list(session.get("asked") or []) + [question["unit_key"]],
                    "asked_count": int(session.get("asked_count") or 0) + 1,
                    "satisfied_count": int(session.get("satisfied_count") or 0) + 1,
                },
            ) or session
            payload.update(_quiz_advance(session))
        else:
            session = store.update_quiz_session(
                session_id, {"question": updated}
            ) or session
            payload.update(
                {
                    "question": quiz.question_view(updated),
                    "finished": False,
                    "session": quiz.session_view(
                        session, store.quiz_coverage(session["source_url"])
                    ),
                }
            )

        return jsonify(payload)

    @app.post("/api/quiz/<int:session_id>/skip")
    def quiz_skip(session_id: int):
        session = store.get_quiz_session(session_id)
        if session is None:
            return jsonify({"error": "Quiz session not found"}), 404

        question = session.get("question")
        if not question:
            return jsonify({"error": "This session has no open question"}), 409

        store.record_quiz_coverage(
            session["source_url"], question["unit_key"], "skipped"
        )
        session = store.update_quiz_session(
            session_id,
            {
                "question": None,
                "asked": list(session.get("asked") or []) + [question["unit_key"]],
                "asked_count": int(session.get("asked_count") or 0) + 1,
            },
        ) or session
        return jsonify(_quiz_advance(session))

    @app.post("/api/quiz/<int:session_id>/next")
    def quiz_next(session_id: int):
        session = store.get_quiz_session(session_id)
        if session is None:
            return jsonify({"error": "Quiz session not found"}), 404

        if session.get("question"):
            return jsonify(
                {
                    "question": quiz.question_view(session["question"]),
                    "finished": False,
                    "session": quiz.session_view(
                        session, store.quiz_coverage(session["source_url"])
                    ),
                }
            )
        return jsonify(_quiz_advance(session))

    # ------------------------------------------------------------------
    # Obsidian reading records (separate from flashcards)
    # ------------------------------------------------------------------
    @app.post("/api/notes")
    def notes_create():
        """Write a reading record to Obsidian. No card, no LLM call."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400

        payload = _capture_payload(data)
        if not (payload["selection"] or payload["focus"] or payload["context"]):
            return jsonify(
                {"error": "Nothing to record — capture a passage or context first"}
            ), 400

        try:
            note = obsidian.write_note(
                payload,
                category=_text(data.get("category"), 80),
            )
        except obsidian.ObsidianError as exc:
            return jsonify({"error": str(exc)}), exc.status
        return jsonify({"note": note}), 201

    @app.post("/api/annotate")
    def annotate():
        """Append a highlight + optional annotation to the source note."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400

        payload = _capture_payload(data)
        if not (payload["selection"] or payload["focus"] or payload["context"]):
            return jsonify(
                {"error": "Nothing to annotate — highlight a passage first"}
            ), 400

        annotation = _text(data.get("annotation"), 3000)
        try:
            note = obsidian.write_note(
                payload,
                annotation=annotation,
                extra_tags=data.get("tags"),
                category=_text(data.get("category"), 80),
            )
        except obsidian.ObsidianError as exc:
            return jsonify({"error": str(exc)}), exc.status
        return jsonify({"note": note}), 201

    @app.post("/api/cards/<int:card_id>/obsidian")
    def card_note(card_id: int):
        """Record an existing card's passage in Obsidian.

        The note contains the source/passage/context, never the card's Q/A.
        """
        card = store.get_card(card_id)
        if card is None:
            return jsonify({"error": "Card not found"}), 404

        try:
            note = obsidian.write_note(_card_note_payload(card), card=card)
        except obsidian.ObsidianError as exc:
            return jsonify({"error": str(exc)}), exc.status

        return jsonify({"note": note, "card": store.get_card(card_id)})

    @app.get("/api/obsidian")
    def obsidian_status():
        data = obsidian.status()
        data["notes"] = store.count_obsidian_notes()
        data["pending"] = store.count_pending_obsidian_notes()
        return jsonify(data)

    @app.get("/api/obsidian/pending")
    def obsidian_pending():
        """Used by the Mac note agent; returns queued obsidian:// URIs."""
        return jsonify(
            {
                "notes": store.pending_obsidian_notes(),
                "count": store.count_pending_obsidian_notes(),
            }
        )

    @app.post("/api/obsidian/ack")
    def obsidian_ack():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400
        try:
            queue_id = int(data.get("id"))
        except (TypeError, ValueError):
            return jsonify({"error": "id is required"}), 400
        if not store.ack_obsidian_note(queue_id):
            return jsonify({"error": "Queued note not found"}), 404
        return jsonify({"ok": True})

    # ------------------------------------------------------------------
    # Spaced repetition
    # ------------------------------------------------------------------
    @app.get("/api/review/next")
    def review_next():
        return jsonify(
            {"card": store.next_review_card(), "stats": store.review_stats()}
        )

    @app.get("/api/review/stats")
    def review_stats():
        return jsonify(store.review_stats())

    @app.post("/api/review/<int:card_id>")
    def review_grade(card_id: int):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Expected a JSON object"}), 400
        try:
            card = store.grade_card(card_id, str(data.get("grade", "")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        if card is None:
            return jsonify({"error": "Card not found"}), 404
        return jsonify({"card": card, "stats": store.review_stats()})

    # ------------------------------------------------------------------
    # Userscript + exports
    # ------------------------------------------------------------------
    @app.get("/userscript.user.js")
    def userscript():
        path = os.path.join(app.static_folder or "", "flashcap.user.js")
        if not os.path.isfile(path):
            return Response("// userscript missing\n", status=404, mimetype="text/javascript")

        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        base = get_public_url() or request.host_url.rstrip("/")
        source = source.replace("__FLASHCAP_BASE__", base)
        return Response(
            source,
            mimetype="application/javascript",
            headers={"Content-Disposition": 'inline; filename="flashcap.user.js"'},
        )

    @app.get("/export/anki.tsv")
    def export_anki():
        cards = store.list_cards(limit=5000)
        lines = [
            "#separator:tab",
            "#html:true",
            "#tags column:3",
            "#deck:Flashcap",
        ]
        for card in cards:
            tags = ["flashcap", f"category::{card['category']}"] + card["tags"]
            lines.append(
                "\t".join(
                    [
                        _html_field(card["front"]),
                        _html_field(card["back"]),
                        " ".join(_anki_tag(tag) for tag in tags),
                    ]
                )
            )
        body = "\n".join(lines) + "\n"
        return Response(
            body,
            mimetype="text/tab-separated-values",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="flashcap-anki-{date.today().isoformat()}.tsv"'
                )
            },
        )

    @app.get("/export/markdown")
    def export_markdown():
        cards = store.list_cards(limit=5000)
        chunks = ["# Flashcap cards", ""]
        for card in cards:
            chunks.append(f"## {card['front']}")
            chunks.append("")
            chunks.append(card["back"])
            chunks.append("")
            meta_bits = [f"`{card['category']}`"]
            meta_bits.extend(f"#{tag}" for tag in card["tags"])
            if card.get("source_url"):
                title = card.get("source_title") or card["source_url"]
                meta_bits.append(f"[{title}]({card['source_url']})")
            chunks.append(" · ".join(meta_bits))
            if card.get("quote"):
                chunks.append("")
                chunks.append(f"> {card['quote']}")
            chunks.append("")
            chunks.append("---")
            chunks.append("")
        return Response(
            "\n".join(chunks),
            mimetype="text/markdown",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="flashcap-{date.today().isoformat()}.md"'
                )
            },
        )

    # ------------------------------------------------------------------
    # API 404 as JSON
    # ------------------------------------------------------------------
    @app.errorhandler(404)
    def not_found(error):  # noqa: ARG001
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not found"}), 404
        return "Not found", 404

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_payload(data: dict) -> dict[str, str]:
    url = _text(data.get("url"), 2000)
    if url and urlparse(url).scheme not in ("http", "https"):
        url = ""

    site = _text(data.get("site"), 200)
    if not site and url:
        site = (urlparse(url).hostname or "").removeprefix("www.")

    selection = _text(data.get("selection"), 4000)
    focus = _text(data.get("focus"), 2500)
    context = _text(data.get("context"), 12000)
    if not context:
        context = _text(data.get("description"), 2000) or focus or selection

    return {
        "selection": selection,
        "focus": focus,
        "context": context,
        "page_title": _text(data.get("page_title"), 400),
        "url": url,
        "site": site,
        "author": _text(data.get("author"), 300),
        "headings": _text(data.get("headings"), 600),
        "source_type": _text(data.get("source_type"), 40) or "web",
        "hint": _text(data.get("hint"), 500),
    }


def _quiz_unit(session: dict, unit_key: object) -> dict | None:
    for unit in session.get("units") or []:
        if unit.get("key") == unit_key:
            return unit
    return None


def _quiz_advance(session: dict) -> dict:
    """Open the next question in *session*, or finish when nothing is left."""
    coverage = store.quiz_coverage(session["source_url"])
    unit = quiz.select_unit(
        session.get("units") or [],
        session.get("asked") or [],
        coverage,
        int(session.get("asked_count") or 0),
    )

    if unit is None:
        current = store.update_quiz_session(session["id"], {"question": None}) or session
        return {
            "question": None,
            "finished": True,
            "session": quiz.session_view(current, coverage),
        }

    try:
        generated = generate_question(
            session.get("source_title") or "", unit["heading"], unit["text"]
        )
    except LLMError as exc:
        # Keep the session open so the panel can offer "Try another question".
        current = store.update_quiz_session(session["id"], {"question": None}) or session
        return {
            "question": None,
            "finished": False,
            "notice": str(exc),
            "session": quiz.session_view(current, coverage),
        }

    question = quiz.build_question(unit, generated)
    store.record_quiz_coverage(session["source_url"], unit["key"])
    current = (
        store.update_quiz_session(session["id"], {"question": question}) or session
    )
    return {
        "question": quiz.question_view(question),
        "finished": False,
        "session": quiz.session_view(current, coverage),
    }


def _int_value(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _card_note_payload(card: dict) -> dict[str, str]:
    """Turn a saved card's provenance into a note payload (no Q/A)."""
    context = card.get("context") or ""
    selection = card.get("selection") or ""
    if not selection:
        selection = context[:2000]
    return {
        "selection": selection,
        "focus": context[:2500],
        "context": context,
        "page_title": card.get("source_title") or "",
        "url": card.get("source_url") or "",
        "site": card.get("source_site") or "",
        "author": card.get("source_author") or "",
        "headings": "",
        "hint": card.get("hint") or "",
        "source_type": "card",
    }


def _text(value: object, limit: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _request_host() -> str:
    """Return the Host header's hostname, without a port."""
    try:
        return (urlparse(f"//{request.host}").hostname or "").lower()
    except ValueError:
        return ""


def _allowed_hosts() -> set[str]:
    """Loopback, plus whichever host this server is configured to serve."""
    hosts = {"127.0.0.1", "localhost", "::1"}
    public = get_public_url()
    if public:
        hosts.add((urlparse(public).hostname or "").lower())
    bound = get_host()
    if bound not in ("0.0.0.0", "::"):
        hosts.add(bound.lower())
    return {host for host in hosts if host}


def _is_local_origin(origin: str) -> bool:
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.hostname in (
        "127.0.0.1",
        "localhost",
        "::1",
    )


def _html_field(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\t", " ").replace("\r", "")
    return html.escape(text, quote=False).replace("\n", "<br>")


def _anki_tag(value: object) -> str:
    tag = str(value or "").strip().lower().replace(" ", "-")
    return "".join(char for char in tag if char.isalnum() or char in "-_:#+/.")[:60]


class _QuietRequestHandler(WSGIRequestHandler):
    """Log the path only: /quick carries the passage in its query string."""

    def log_request(self, code: str | int = "-", size: str | int = "-") -> None:
        path = getattr(self, "path", "").split("?", 1)[0]
        command = getattr(self, "command", "-")
        self.log("info", '"%s %s" %s %s', command, path, code, size)


def main() -> None:
    # The database holds private reading material: create it owner-only.
    os.umask(0o077)
    host = get_host()
    port = get_port()
    app = create_app()
    config = get_llm_config()
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"Flashcap {__version__} → http://{shown}:{port}")
    print(f"  database: {app.config['DB_PATH']}")
    if config["api_key"]:
        print(f"  llm:      {config['model']} via {config['base_url']}")
    else:
        print("  llm:      not configured (captures will be saved unclassified)")
    print(f"  userscript: http://{shown}:{port}/userscript.user.js")
    telegram_config = get_telegram_config()
    if telegram_config["enabled"]:
        threading.Thread(
            target=telegram.run_forever,
            args=(app,),
            name="telegram-poll",
            daemon=True,
        ).start()
        print("  telegram:  polling for reading-list links")
    else:
        print("  telegram:  not configured (set TELEGRAM_BOT_TOKEN)")
    app.run(
        host=host,
        port=port,
        debug=False,
        threaded=True,
        use_reloader=False,
        request_handler=_QuietRequestHandler,
    )


if __name__ == "__main__":
    main()
