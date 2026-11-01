"""Flashcap local server.

Run from the project root::

    python -m capture.server

The server binds to 127.0.0.1 only. It has no accounts and no remote access;
the userscript talks to it over localhost.
"""

from __future__ import annotations

import html
import os
from datetime import date
from urllib.parse import urlparse

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
)

from . import __version__, obsidian, store
from .config import get_db_path, get_host, get_llm_config, get_port, get_public_url
from .llm import LLMError, explain_passage, suggest_metadata
from .taxonomy import UI_CATEGORIES


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
        return jsonify(
            {
                "ok": True,
                "version": __version__,
                "cards": store.metadata()["total"],
                "llm_configured": bool(config["api_key"]),
                "model": config["model"],
            }
        )

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


def main() -> None:
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
    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
