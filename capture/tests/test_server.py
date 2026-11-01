"""Tests for the Flashcap local server and LLM wrapper."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from types import SimpleNamespace

from capture import llm, note_agent, obsidian, server, store


@pytest.fixture()
def app(tmp_path):
    application = server.create_app(str(tmp_path / "cards.db"))
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture()
def obsidian_stub(monkeypatch):
    """Capture outbound Obsidian URIs instead of handing them to the app."""
    sent: list[str] = []
    monkeypatch.setattr(obsidian, "run_uri", lambda uri: sent.append(uri))
    monkeypatch.setattr(
        obsidian,
        "registered_vaults",
        lambda: [{"id": "vault-writing", "name": "Writing", "path": "/tmp/Writing"}],
    )
    monkeypatch.setattr(
        obsidian,
        "get_obsidian_config",
        lambda: {"enabled": True, "vault": "Writing", "folder": "Reading"},
    )
    return sent


def uri_query(uri: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(uri).query)


def capture(client, **overrides):
    payload = {
        "selection": "Attention lets a model weight tokens by relevance.",
        "focus": "Attention lets a model weight tokens by relevance. It is the core of the transformer.",
        "context": "Attention lets a model weight tokens by relevance. It is the core of the transformer. "
        "Multi-head attention runs several attention operations in parallel.",
        "page_title": "Attention Is All You Need",
        "url": "https://example.com/attention",
        "site": "example.com",
        "source_type": "web",
        "hint": "",
        "front": "What does attention do?",
        "back": "It weights tokens by relevance.",
        "category": "machine-learning",
        "tags": ["attention", "transformers"],
    }
    payload.update(overrides)
    return client.post("/api/capture", json=payload)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def test_capture_saves_manual_card(client):
    response = capture(
        client,
        front="  What does attention do?  ",
        back="  It weights tokens by relevance.  ",
        category="Machine Learning",
        tags="attention, transformers",
    )
    assert response.status_code == 201

    card = response.get_json()["card"]
    assert card["front"] == "What does attention do?"
    assert card["back"] == "It weights tokens by relevance."
    assert card["category"] == "machine-learning"
    assert card["tags"] == ["attention", "transformers"]
    assert card["source_title"] == "Attention Is All You Need"
    assert card["source_url"] == "https://example.com/attention"
    assert card["generated_by"] == "manual"
    assert card["confidence"] is None


def test_capture_requires_front_and_back(client):
    assert client.post("/api/capture", json={}).status_code == 400
    response = client.post("/api/capture", json={"front": "Only a front"})
    assert response.status_code == 400
    assert "Front and back" in response.get_json()["error"]


def test_capture_rejects_non_json(client):
    response = client.post("/api/capture", data="not json", content_type="text/plain")
    assert response.status_code == 400


def test_capture_derives_site_and_drops_javascript_url(client):
    response = capture(
        client,
        url="javascript:alert(1)",
        site="",
        selection="A useful passage",
    )
    assert response.status_code == 201
    card = response.get_json()["card"]
    assert card["source_url"] is None
    assert card["source_site"] is None

    response = capture(client, site="", url="https://www.example.org/post")
    card = response.get_json()["card"]
    assert card["source_site"] == "example.org"


# ---------------------------------------------------------------------------
# Editing + deleting
# ---------------------------------------------------------------------------

def test_patch_normalises_tags_and_category(client):
    card = capture(client).get_json()["card"]
    response = client.patch(
        f"/api/cards/{card['id']}",
        json={
            "front": "  A better question?  ",
            "category": "Machine Learning",
            "tags": "Linear Algebra, attention, linear-algebra",
        },
    )
    assert response.status_code == 200
    updated = response.get_json()["card"]
    assert updated["front"] == "A better question?"
    assert updated["category"] == "machine-learning"
    assert updated["tags"] == ["linear-algebra", "attention"]


def test_patch_rejects_empty_front(client):
    card = capture(client).get_json()["card"]
    response = client.patch(f"/api/cards/{card['id']}", json={"front": "   "})
    assert response.status_code == 400


def test_patch_missing_card_returns_404(client):
    response = client.patch("/api/cards/9999", json={"front": "Q?"})
    assert response.status_code == 404


def test_delete_card(client):
    card = capture(client).get_json()["card"]
    assert client.delete(f"/api/cards/{card['id']}").status_code == 200
    assert client.get(f"/api/cards/{card['id']}").status_code == 404
    assert client.delete(f"/api/cards/{card['id']}").status_code == 404


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def test_list_search_and_filters(client):
    capture(client, selection="transformers use attention")
    capture(
        client,
        selection="saddlepoint finding beats sorting",
        front="Why is the saddlepoint faster than sorting?",
        category="algorithms",
        tags=["saddlepoint", "sorting"],
    )

    all_cards = client.get("/api/cards").get_json()
    assert all_cards["count"] == 2

    search = client.get("/api/cards?q=saddlepoint").get_json()
    assert search["count"] == 1
    assert "saddlepoint" in search["cards"][0]["front"].lower()

    category = client.get("/api/cards?category=algorithms").get_json()
    assert category["count"] == 1

    tag = client.get("/api/cards?tag=attention").get_json()
    assert tag["count"] == 1
    assert tag["cards"][0]["category"] == "machine-learning"


def test_meta_counts(client):
    capture(client)
    capture(client, selection="A second passage about attention")
    meta = client.get("/api/meta").get_json()

    assert meta["total"] == 2
    assert meta["categories"]["machine-learning"] == 2
    assert meta["tags"]["attention"] == 2
    assert "machine-learning" in meta["category_choices"]
    assert meta["llm"]["configured"] is True


def test_health(client):
    data = client.get("/api/health").get_json()
    assert data["ok"] is True
    assert data["cards"] == 0
    assert data["version"]


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def test_anki_export_escapes_html_and_tags(client):
    monkeypatch_card = capture(client).get_json()["card"]
    client.patch(
        f"/api/cards/{monkeypatch_card['id']}",
        json={"front": "Is <b>this</b> safe?", "back": "Line one\nLine two"},
    )

    response = client.get("/export/anki.tsv")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    lines = text.splitlines()

    assert lines[0] == "#separator:tab"
    assert lines[1] == "#html:true"
    assert lines[2] == "#tags column:3"
    assert "category::machine-learning" in text
    assert "flashcap" in text
    assert "&lt;b&gt;this&lt;/b&gt;" in text
    assert "Line one<br>Line two" in text


def test_markdown_export(client):
    capture(client)
    response = client.get("/export/markdown")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "# Flashcap cards" in text
    assert "`machine-learning`" in text
    assert "#attention" in text


# ---------------------------------------------------------------------------
# Userscript + pages
# ---------------------------------------------------------------------------

def test_userscript_injects_base_url(client):
    response = client.get("/userscript.user.js")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "__FLASHCAP_BASE__" not in text
    assert "const BASE = 'http://localhost'" in text
    assert "GM.xmlHttpRequest" in text
    assert "api/capture" in text


def test_pages_render(client):
    assert "Cards from what you read" in client.get("/").get_data(as_text=True)
    assert "Capture without the page" in client.get("/quick").get_data(as_text=True)


def test_api_404_is_json(client):
    response = client.get("/api/nope")
    assert response.status_code == 404
    assert response.is_json


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

def test_cors_only_for_local_origins(client):
    local = client.get("/api/cards", headers={"Origin": "http://127.0.0.1:8765"})
    assert local.headers.get("Access-Control-Allow-Origin") == "http://127.0.0.1:8765"

    foreign = client.get("/api/cards", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in foreign.headers


def test_preflight_for_local_origin(client):
    response = client.options(
        "/api/cards",
        headers={
            "Origin": "http://127.0.0.1:8765",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )
    assert response.status_code == 204
    assert response.headers.get("Access-Control-Allow-Methods")


# ---------------------------------------------------------------------------
# Metadata suggestions (LLM labels only — never card text)
# ---------------------------------------------------------------------------

def test_suggest_endpoint_returns_metadata(client, monkeypatch):
    monkeypatch.setattr(
        server,
        "suggest_metadata",
        lambda payload, known_tags=None: {
            "category": "machine-learning",
            "tags": ["attention", "transformers"],
            "model": "test-model",
        },
    )
    response = client.post("/api/suggest", json=note_body())
    assert response.status_code == 200
    assert response.get_json()["tags"] == ["attention", "transformers"]
    assert "front" not in response.get_json()


def test_suggest_requires_a_passage(client):
    response = client.post("/api/suggest", json={"page_title": "Only a title"})
    assert response.status_code == 400


def test_suggest_error_returns_502(client, monkeypatch):
    def fail(_payload, known_tags=None):
        raise llm.LLMError("provider is down")

    monkeypatch.setattr(server, "suggest_metadata", fail)
    response = client.post("/api/suggest", json=note_body())
    assert response.status_code == 502
    assert "provider is down" in response.get_json()["error"]


def test_suggest_metadata_parses_json_and_tags(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_llm_config",
        lambda: {"api_key": "k", "base_url": "http://example", "model": "m"},
    )
    monkeypatch.setattr(
        llm,
        "_call_llm",
        lambda *args, **kwargs: '```json\n{"category": "Machine Learning", "tags": "attention, transformers"}\n```',
    )
    result = llm.suggest_metadata(
        {"selection": "Attention connects tokens directly.", "page_title": "Attention"}
    )
    assert result["category"] == "machine-learning"
    assert result["tags"] == ["attention", "transformers"]
    assert set(result) == {"category", "tags", "model"}


def test_metadata_prompt_forbids_card_text():
    prompt = llm._build_metadata_prompt(
        {"selection": "Attention connects tokens directly.", "page_title": "Attention"}
    )
    assert "Never write a question" in prompt
    assert "category" in prompt
    assert "tags" in prompt


def test_suggest_metadata_only_reuses_existing_tags(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_llm_config",
        lambda: {"api_key": "k", "base_url": "http://example", "model": "m"},
    )
    monkeypatch.setattr(
        llm,
        "_call_llm",
        lambda *args, **kwargs: '{"category": "machine-learning", "tags": ["attention", "brand-new-tag", "transformers", "another-new"]}',
    )
    result = llm.suggest_metadata(
        {"selection": "Attention connects tokens directly."},
        known_tags={"attention": 5, "transformers": 3},
    )
    assert result["tags"] == ["attention", "transformers"]


def test_suggest_metadata_rejects_invented_category(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_llm_config",
        lambda: {"api_key": "k", "base_url": "http://example", "model": "m"},
    )
    monkeypatch.setattr(
        llm,
        "_call_llm",
        lambda *args, **kwargs: '{"category": "deep-learning", "tags": []}',
    )
    result = llm.suggest_metadata({"selection": "Attention."}, known_tags={})
    assert result["category"] == "other"


def test_suggest_metadata_seeds_at_most_two_when_vocabulary_empty(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_llm_config",
        lambda: {"api_key": "k", "base_url": "http://example", "model": "m"},
    )
    monkeypatch.setattr(
        llm,
        "_call_llm",
        lambda *args, **kwargs: '{"category": "other", "tags": ["one", "two", "three", "four"]}',
    )
    result = llm.suggest_metadata(
        {"selection": "A new topic."},
        known_tags={},
    )
    assert result["tags"] == ["one", "two"]


def test_suggest_endpoint_passes_tag_vocabulary(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(store, "tag_vocabulary", lambda limit=40: {"attention": 7})
    monkeypatch.setattr(
        server,
        "suggest_metadata",
        lambda payload, known_tags=None: seen.update(known_tags) or {"category": "other", "tags": ["attention"], "model": "m"},
    )
    response = client.post("/api/suggest", json=note_body())
    assert response.status_code == 200
    assert seen == {"attention": 7}


# ---------------------------------------------------------------------------
# Inline explanation (the other LLM call)
# ---------------------------------------------------------------------------

def test_explain_prompt_includes_passage_and_context():
    prompt = llm._build_explain_prompt(
        {
            "selection": "Self-attention has path length one.",
            "context": "Every token can attend to every other token directly.",
            "page_title": "Attention Is All You Need",
            "headings": "3.2 Attention",
        }
    )
    assert "Self-attention has path length one." in prompt
    assert "Every token can attend to every other token directly." in prompt
    assert "3.2 Attention" in prompt


def test_clean_explanation_strips_fences():
    assert llm._clean_explanation("```\nA plain explanation.\n```") == "A plain explanation."


def test_explain_endpoint_returns_explanation(client, monkeypatch):
    monkeypatch.setattr(
        server,
        "explain_passage",
        lambda payload: {
            "explanation": "Attention gives every token a direct path to every other token.",
            "model": "test-model",
            "generated_by": "llm",
        },
    )
    response = client.post("/api/explain", json=note_body())
    assert response.status_code == 200
    body = response.get_json()
    assert "direct path" in body["explanation"]
    assert body["model"] == "test-model"


def test_explain_requires_a_passage(client, monkeypatch):
    called = []
    monkeypatch.setattr(
        server,
        "explain_passage",
        lambda payload: called.append(payload) or {"explanation": "nope"},
    )
    response = client.post("/api/explain", json={"page_title": "Just a title"})
    assert response.status_code == 400
    assert not called


def test_explain_error_returns_502(client, monkeypatch):
    def fail(_payload, known_tags=None):
        raise llm.LLMError("provider is down")

    monkeypatch.setattr(server, "explain_passage", fail)
    response = client.post("/api/explain", json=note_body())
    assert response.status_code == 502
    assert "provider is down" in response.get_json()["error"]


def test_extract_text_skips_thinking_blocks():
    text = llm._extract_text(
        {
            "content": [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "A plain explanation."},
            ]
        }
    )
    assert text == "A plain explanation."


def test_extract_text_requires_text_block():
    with pytest.raises(llm.LLMError):
        llm._extract_text({"content": [{"type": "thinking", "thinking": "..."}]})


# ---------------------------------------------------------------------------
# Obsidian reading notes (deliberately separate from cards)
# ---------------------------------------------------------------------------

def note_body(**overrides):
    payload = {
        "selection": "Self-attention lets each token attend to every other token in the sequence.",
        "focus": "Self-attention lets each token attend to every other token in the sequence. "
        "It is the core of the transformer.",
        "context": "Self-attention lets each token attend to every other token in the sequence. "
        "It is the core of the transformer. Multi-head attention runs several "
        "attention operations in parallel.",
        "page_title": "Attention Is All You Need",
        "url": "https://arxiv.org/abs/1706.03762?utm_source=test",
        "site": "arxiv.org",
        "author": "Vaswani et al.",
        "headings": "3.2 Attention",
        "source_type": "web",
    }
    payload.update(overrides)
    return payload


def test_note_requires_obsidian_to_be_enabled(client, monkeypatch):
    monkeypatch.setattr(
        obsidian,
        "get_obsidian_config",
        lambda: {"enabled": False, "vault": "", "folder": "Reading"},
    )
    response = client.post("/api/notes", json=note_body())
    assert response.status_code == 400
    assert "FLASHCAP_OBSIDIAN_VAULT" in response.get_json()["error"]


def test_note_requires_a_passage(client, obsidian_stub):
    response = client.post("/api/notes", json={"page_title": "Only a title"})
    assert response.status_code == 400
    assert not obsidian_stub


def test_note_creates_literature_note_and_appends(client, obsidian_stub):
    response = client.post("/api/notes", json=note_body())
    assert response.status_code == 201

    note = response.get_json()["note"]
    assert note["vault"] == "Writing"
    assert note["path"] == "Reading/Attention Is All You Need.md"
    assert note["appended"] is False
    assert note["open_url"].startswith("obsidian://open?")

    first_uri = obsidian_stub[0]
    query = uri_query(first_uri)
    assert first_uri.startswith("obsidian://new?")
    assert query["silent"] == ["true"]
    assert "append" not in query

    content = query["content"][0]
    assert "Self-attention lets each token" in content
    assert "## Highlights" in content
    assert "### " in content
    assert "# Attention Is All You Need" not in content
    assert "## My notes" not in content
    assert "[!info] Source" not in content
    assert "#reading" not in content
    assert "#flashcap" not in content
    assert "3.2 Attention" not in content
    assert "title:" in content
    assert "  - reading" in content
    assert "  - flashcap" in content
    assert "question" not in content.lower()
    assert "answer" not in content.lower()

    # A second capture from the same source appends a section, not a new note.
    response = client.post("/api/notes", json=note_body())
    assert response.status_code == 201
    assert response.get_json()["note"]["appended"] is True

    second_uri = obsidian_stub[1]
    query = uri_query(second_uri)
    assert query["append"] == ["true"]
    second_content = query["content"][0]
    assert second_content.strip().startswith(">")
    assert "###" not in second_content
    assert "title:" not in second_content
    assert "## My notes" not in second_content

    status = client.get("/api/obsidian").get_json()
    assert status["enabled"] is True
    assert status["vault"] == "Writing"
    assert status["notes"] == 1


def test_note_is_minimal_by_default(client, obsidian_stub):
    response = client.post("/api/annotate", json=note_body(annotation="A short note."))
    assert response.status_code == 201
    content = uri_query(obsidian_stub[0])["content"][0]

    assert "## Highlights" in content
    assert "## My notes" not in content
    assert "[!info] Source" not in content
    assert "#reading" not in content
    assert "\n-\n" not in content
    assert "Surrounding context" not in content
    assert "Context (excerpt)" not in content


def test_note_context_can_be_enabled(client, monkeypatch, obsidian_stub):
    monkeypatch.setenv("FLASHCAP_NOTE_CONTEXT", "1")
    response = client.post("/api/annotate", json=note_body(annotation="A short note."))
    assert response.status_code == 201
    content = uri_query(obsidian_stub[0])["content"][0]
    assert "Context (excerpt)" in content


def test_card_note_contains_provenance_but_not_qa(client, obsidian_stub):
    card = capture(
        client,
        front="ZZZ_CARD_FRONT_ZZZ?",
        back="ZZZ_CARD_BACK_ZZZ.",
        tags=["self-attention", "transformer"],
    ).get_json()["card"]

    response = client.post(f"/api/cards/{card['id']}/obsidian")
    assert response.status_code == 200
    data = response.get_json()
    note = data["note"]
    assert note["path"] == "Reading/Attention Is All You Need.md"
    assert data["card"]["obsidian_url"] == note["open_url"]
    assert data["card"]["obsidian_path"] == note["path"]

    content = uri_query(obsidian_stub[0])["content"][0]
    assert "ZZZ_CARD_FRONT_ZZZ" not in content
    assert "ZZZ_CARD_BACK_ZZZ" not in content
    assert "Attention lets a model weight tokens" in content
    assert 'category: "machine-learning"' in content
    assert "  - machine-learning" in content
    assert "  - self-attention" in content
    assert "#machine-learning" not in content

    # Clicking Note again opens the existing record instead of duplicating it.
    response = client.post(f"/api/cards/{card['id']}/obsidian")
    assert response.status_code == 200
    assert response.get_json()["note"]["already"] is True
    assert len(obsidian_stub) == 1


def test_annotate_appends_annotation_and_tags(client, obsidian_stub):
    response = client.post(
        "/api/annotate",
        json=note_body(
            annotation="This is the key distinction: attention is a direct path, not a recurrence.",
            category="machine-learning",
            tags="attention, mechanism, my-tag",
        ),
    )
    assert response.status_code == 201
    note = response.get_json()["note"]
    assert note["path"] == "Reading/Attention Is All You Need.md"
    assert note["appended"] is False

    content = uri_query(obsidian_stub[0])["content"][0]
    assert "type: literature-note" in content
    assert 'category: "machine-learning"' in content
    assert "> [!note]" not in content
    assert "This is the key distinction" in content
    assert "  - machine-learning" in content
    assert "  - my-tag" in content
    assert "  - mechanism" in content
    assert "#my-tag" not in content
    assert "## My notes" not in content

    response = client.post(
        "/api/annotate",
        json=note_body(
            selection="Recurrent networks must pass information through every step.",
            annotation="This is the cost that self-attention removes.",
            tags="recurrence",
        ),
    )
    assert response.status_code == 201
    assert response.get_json()["note"]["appended"] is True

    content = uri_query(obsidian_stub[1])["content"][0]
    assert content.strip().startswith(">")
    assert "> [!note]" not in content
    assert "This is the cost that self-attention removes." in content
    assert "#recurrence" not in content
    assert "title:" not in content
    assert "###" not in content


def test_note_tags_feed_the_shared_vocabulary(client, obsidian_stub):
    response = client.post(
        "/api/annotate",
        json=note_body(
            category="algorithms",
            tags="sorting, heaps, ai-explanation",
        ),
    )
    assert response.status_code == 201
    vocabulary = store.tag_vocabulary()
    assert vocabulary["sorting"] == 1
    assert vocabulary["heaps"] == 1
    assert vocabulary["algorithms"] == 1
    assert "ai-explanation" not in vocabulary
    assert "reading" not in vocabulary


def test_annotate_requires_a_passage(client, obsidian_stub):
    response = client.post(
        "/api/annotate",
        json={"annotation": "A note without a highlighted passage"},
    )
    assert response.status_code == 400
    assert not obsidian_stub


def test_note_uses_normalized_source_url_as_identity(client, obsidian_stub):
    first = note_body(url="https://www.Example.com/a/?utm_source=x#frag")
    second = note_body(url="https://example.com/a/?fbclid=y")
    client.post("/api/notes", json=first)
    client.post("/api/notes", json=second)

    assert uri_query(obsidian_stub[1])["append"] == ["true"]
    assert obsidian.normalize_url(first["url"]) == "https://example.com/a"


def test_queue_mode_queues_uri_without_opening(client, monkeypatch, obsidian_stub):
    monkeypatch.setattr(
        obsidian,
        "get_obsidian_config",
        lambda: {
            "enabled": True,
            "vault": "Writing",
            "folder": "Reading",
            "mode": "queue",
        },
    )
    response = client.post("/api/notes", json=note_body())
    assert response.status_code == 201
    note = response.get_json()["note"]
    assert note["queued"] is True
    assert not obsidian_stub  # run_uri must not be called on the server

    pending = client.get("/api/obsidian/pending").get_json()
    assert pending["count"] == 1
    queued = pending["notes"][0]
    assert queued["uri"].startswith("obsidian://new?")
    assert queued["path"] == "Reading/Attention Is All You Need.md"

    acked = client.post("/api/obsidian/ack", json={"id": queued["id"]})
    assert acked.status_code == 200
    assert client.get("/api/obsidian/pending").get_json()["count"] == 0


def test_note_agent_drains_queue_in_order(monkeypatch):
    notes = [
        {"id": 1, "uri": "obsidian://new?one", "path": "Reading/A.md"},
        {"id": 2, "uri": "obsidian://new?two", "path": "Reading/A.md"},
    ]
    acked: list[int] = []
    opened: list[str] = []

    monkeypatch.setattr(note_agent, "pending", lambda: notes)
    monkeypatch.setattr(note_agent, "ack", lambda queue_id: acked.append(queue_id))
    monkeypatch.setattr(note_agent.time, "sleep", lambda _seconds: None)

    def fake_open(args, **_kwargs):
        opened.append(args[-1])
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(note_agent.subprocess, "run", fake_open)

    assert note_agent.drain() == 2
    assert opened == ["obsidian://new?one", "obsidian://new?two"]
    assert acked == [1, 2]


def test_practice_new_card_and_grade(client):
    card = capture(client).get_json()["card"]

    queue = client.get("/api/review/next").get_json()
    assert queue["card"]["id"] == card["id"]
    assert queue["stats"]["new"] == 1
    assert queue["stats"]["available"] == 1

    response = client.post(
        f"/api/review/{card['id']}", json={"grade": "good"}
    )
    assert response.status_code == 200
    reviewed = response.get_json()["card"]
    assert reviewed["reps"] == 1
    assert reviewed["srs_state"] == 1  # FSRS learning state
    assert reviewed["stability"] > 0
    assert 0 < reviewed["interval_days"] < 1
    assert reviewed["due_at"]
    assert response.get_json()["stats"]["available"] == 0
    assert response.get_json()["stats"]["reviewed_today"] == 1
    assert response.get_json()["stats"]["scheduler"] == "fsrs"

    assert client.get("/api/review/next").get_json()["card"] is None
    assert "Practice" in client.get("/practice").get_data(as_text=True)


def test_review_rejects_invalid_grade(client):
    card = capture(client).get_json()["card"]
    response = client.post(
        f"/api/review/{card['id']}", json={"grade": "perfect"}
    )
    assert response.status_code == 400


def test_review_again_reschedules_in_minutes(client):
    card = capture(client).get_json()["card"]
    response = client.post(f"/api/review/{card['id']}", json={"grade": "again"})
    reviewed = response.get_json()["card"]
    assert reviewed["lapses"] == 1
    assert reviewed["interval_days"] < 0.01
    assert reviewed["due_at"]


def test_note_path_sanitizes_titles():
    path = obsidian.note_path(
        {"page_title": 'A/B: "C"? #1', "site": "example.com"}, "Reading"
    )
    assert path.startswith("Reading/")
    assert path.endswith(".md")
    name = path.removeprefix("Reading/").removesuffix(".md")
    for forbidden in '/\\:*?"<>|#^[]':
        assert forbidden not in name
    assert name.strip()
