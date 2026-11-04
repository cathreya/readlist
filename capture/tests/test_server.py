"""Tests for the Flashcap local server and LLM wrapper."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from types import SimpleNamespace

from capture import llm, note_agent, obsidian, quiz, server, store


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


def test_api_rejects_unexpected_host(client):
    assert client.get("/api/health", headers={"Host": "evil.example:8765"}).status_code == 403
    assert client.get("/api/health").status_code == 200


def test_note_agent_refuses_unexpected_uris(monkeypatch):
    notes = [
        {"id": 1, "uri": "file:///etc/passwd", "path": "Reading/A.md"},
        {"id": 2, "uri": "obsidian://new?ok", "path": "../escape.md"},
        {"id": 3, "uri": "obsidian://new?ok", "path": "Reading/A.md"},
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

    assert note_agent.drain() == 1
    assert opened == ["obsidian://new?ok"]
    assert acked == [1, 2, 3]


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


# ---------------------------------------------------------------------------
# Quiz me
# ---------------------------------------------------------------------------

def quiz_unit(index: int) -> dict:
    return {
        "key": f"u{index}",
        "text": f"Passage {index} " + "word " * 60,
        "heading": f"Section {index}",
    }


def start_quiz(client, units=None, monkeypatch=None, url="https://example.com/long-read"):
    units = units if units is not None else [quiz_unit(i) for i in range(4)]
    return client.post(
        "/api/quiz/start",
        json={
            "url": url,
            "page_title": "A long read",
            "units": units,
            "total_units": len(units),
        },
    )


def fake_question(title, heading, passage):
    return {
        "question": f"What does {heading or 'the passage'} claim?",
        "kind": "explain",
        "expected": ["the main claim", "why it matters"],
        "hints": ["Look at the first sentence.", "It contrasts two things.", "It is about the mechanism."],
    }


def test_quiz_normalize_units_drops_short_and_duplicate_passages():
    units = quiz.normalize_units(
        [
            {"key": "a", "text": "too short"},
            {"key": "a", "text": "x" * 250},
            {"key": "b", "text": "x" * 250},
            {"key": "a", "text": "x" * 250},
            "not a unit",
        ]
    )
    assert [unit["key"] for unit in units] == ["a", "b"]


def test_quiz_select_prefers_the_passage_just_read():
    units = [quiz_unit(i) for i in range(4)]
    picked = quiz.select_unit(units, [], {}, 0)
    assert picked["key"] == "u3"


def test_quiz_select_mixes_recall_every_fourth_question():
    units = [quiz_unit(i) for i in range(4)]
    coverage = {
        "u1": {"last_asked_at": "2026-01-01T00:00:00Z"},
        "u2": {"last_asked_at": "2026-02-01T00:00:00Z"},
    }
    fresh = quiz.select_unit(units, [], coverage, 0)
    assert fresh["key"] == "u3"

    recall = quiz.select_unit(units, [], coverage, 3)
    assert recall["key"] == "u1"  # stalest coverage first


def test_quiz_select_skips_passages_asked_this_session():
    units = [quiz_unit(i) for i in range(3)]
    picked = quiz.select_unit(units, ["u2"], {}, 1)
    assert picked["key"] == "u1"


def test_quiz_select_finishes_when_everything_was_asked():
    units = [quiz_unit(i) for i in range(2)]
    assert quiz.select_unit(units, ["u0", "u1"], {}, 2) is None


def test_quiz_hint_ladder_escalates_then_reveals_the_answer():
    question = quiz.build_question(quiz_unit(0), fake_question("", "", ""))

    question, first = quiz.apply_grade(
        question, {"satisfactory": False, "feedback": "Too vague."}
    )
    assert first["verdict"] == "not_yet"
    assert first["hint"] == {"level": 1, "text": "Look at the first sentence."}
    assert first["revealed"] is False

    question, second = quiz.apply_grade(
        question, {"satisfactory": False, "feedback": "Still vague."}
    )
    assert second["hint"]["level"] == 2

    question, third = quiz.apply_grade(
        question, {"satisfactory": False, "feedback": "Missing the point."}
    )
    assert third["hint"]["level"] == 3

    question, fourth = quiz.apply_grade(
        question, {"satisfactory": False, "feedback": "Not yet."}
    )
    assert fourth["hint"] is None
    assert fourth["revealed"] is True
    assert fourth["answer"] == ["the main claim", "why it matters"]

    _question, accepted = quiz.apply_grade(
        question, {"satisfactory": True, "feedback": "That is it."}
    )
    assert accepted["verdict"] == "satisfactory"


def test_quiz_question_view_hides_expected_points_and_hints():
    question = quiz.build_question(quiz_unit(0), fake_question("", "", ""))
    view = quiz.question_view(question)
    assert view["text"].startswith("What does")
    assert "expected" not in view
    assert "hints" not in view


def test_quiz_api_requires_a_passage_above_the_reader(client, monkeypatch):
    monkeypatch.setattr(server, "generate_question", fake_question)
    response = start_quiz(client, units=[{"key": "u0", "text": "short", "heading": ""}])
    assert response.status_code == 422


def test_quiz_api_returns_a_question_without_the_answer(client, monkeypatch):
    monkeypatch.setattr(server, "generate_question", fake_question)
    response = start_quiz(client)
    assert response.status_code == 201
    body = response.get_json()
    assert body["question"]["text"].startswith("What does")
    assert "expected" not in body["question"]
    assert body["session"]["read_units"] == 4
    assert body["session"]["fresh_remaining"] == 3
    assert body["session"]["asked_count"] == 0


def test_quiz_api_wrong_answer_hints_and_keeps_the_question(client, monkeypatch):
    monkeypatch.setattr(server, "generate_question", fake_question)
    monkeypatch.setattr(
        server,
        "grade_answer",
        lambda *args, **kwargs: {"satisfactory": False, "feedback": "Say why."},
    )
    start = start_quiz(client).get_json()
    session_id = start["session"]["id"]
    question_text = start["question"]["text"]

    response = client.post(
        f"/api/quiz/{session_id}/answer", json={"answer": "It is about stuff."}
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["verdict"] == "not_yet"
    assert body["feedback"] == "Say why."
    assert body["hint"]["level"] == 1
    assert body["question"]["text"] == question_text
    assert body["session"]["asked_count"] == 0


def test_quiz_api_satisfactory_answer_advances_and_records_coverage(client, monkeypatch):
    seen: list[str] = []

    def generate(title, heading, passage):
        seen.append(passage)
        return fake_question(title, heading, passage)

    monkeypatch.setattr(server, "generate_question", generate)
    monkeypatch.setattr(
        server,
        "grade_answer",
        lambda *args, **kwargs: {"satisfactory": True, "feedback": "Correct."},
    )

    first = start_quiz(client).get_json()
    assert seen[0].startswith("Passage 3")

    response = client.post(
        f"/api/quiz/{first['session']['id']}/answer",
        json={"answer": "It claims attention is enough."},
    )
    body = response.get_json()
    assert body["verdict"] == "satisfactory"
    assert body["session"]["asked_count"] == 1
    assert body["session"]["satisfied_count"] == 1
    assert body["session"]["fresh_remaining"] == 2
    assert body["question"]["text"].startswith("What does")
    assert seen[1].startswith("Passage 2")

    second = start_quiz(client)
    assert second.status_code == 201
    assert seen[2].startswith("Passage 2") or seen[2].startswith("Passage 1")


def test_quiz_api_second_session_skips_already_quizzed_passages(client, monkeypatch):
    seen: list[str] = []

    def generate(title, heading, passage):
        seen.append(passage)
        return fake_question(title, heading, passage)

    monkeypatch.setattr(server, "generate_question", generate)
    monkeypatch.setattr(
        server,
        "grade_answer",
        lambda *args, **kwargs: {"satisfactory": True, "feedback": "Correct."},
    )

    first = start_quiz(client).get_json()
    client.post(
        f"/api/quiz/{first['session']['id']}/answer",
        json={"answer": "Yes."},
    )
    start_quiz(client)
    assert not seen[-1].startswith("Passage 3")


def test_quiz_api_grading_failure_does_not_consume_the_attempt(client, monkeypatch):
    monkeypatch.setattr(server, "generate_question", fake_question)

    def fail(*args, **kwargs):
        raise llm.LLMError("provider is down")

    monkeypatch.setattr(server, "grade_answer", fail)
    start = start_quiz(client).get_json()
    session_id = start["session"]["id"]

    response = client.post(
        f"/api/quiz/{session_id}/answer", json={"answer": "An answer."}
    )
    assert response.status_code == 502

    resumed = client.post(f"/api/quiz/{session_id}/next")
    assert resumed.status_code == 200
    assert resumed.get_json()["question"]["text"] == start["question"]["text"]


def test_quiz_api_skip_records_coverage_and_advances(client, monkeypatch):
    monkeypatch.setattr(server, "generate_question", fake_question)
    start = start_quiz(client).get_json()

    response = client.post(f"/api/quiz/{start['session']['id']}/skip")
    body = response.get_json()
    assert response.status_code == 200
    assert body["session"]["asked_count"] == 1
    assert body["session"]["satisfied_count"] == 0
    assert body["question"]["text"].startswith("What does")


def test_quiz_api_unknown_session_is_404(client):
    assert client.post("/api/quiz/999/answer", json={"answer": "x"}).status_code == 404


def test_quiz_api_requires_an_answer(client, monkeypatch):
    monkeypatch.setattr(server, "generate_question", fake_question)
    session_id = start_quiz(client).get_json()["session"]["id"]
    response = client.post(f"/api/quiz/{session_id}/answer", json={"answer": "   "})
    assert response.status_code == 400


def test_quiz_generate_parses_json_and_pads_missing_hints(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        llm,
        "_call_llm",
        lambda *args, **kwargs: (
            '```json\n{"question": "Why?", "kind": "explain", '
            '"expected": ["because"], "hints": []}\n```'
        ),
    )
    result = llm.generate_question("Title", "Heading", "A passage.")
    assert result["question"] == "Why?"
    assert result["hints"] == [llm.FALLBACK_HINT]


def test_quiz_generate_rejects_an_empty_question(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        llm, "_call_llm", lambda *args, **kwargs: '{"question": "", "expected": ["x"]}'
    )
    with pytest.raises(llm.LLMError):
        llm.generate_question("Title", "Heading", "A passage.")


def test_quiz_grade_parses_a_boolean_verdict(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        llm,
        "_call_llm",
        lambda *args, **kwargs: '{"satisfactory": "false", "feedback": "Missing the why."}',
    )
    result = llm.grade_answer("Why?", ["because"], "A passage.", "It happens.")
    assert result["satisfactory"] is False
    assert result["feedback"] == "Missing the why."


def test_quiz_grade_rejects_a_missing_verdict(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(llm, "_call_llm", lambda *args, **kwargs: '{"feedback": "hmm"}')
    with pytest.raises(llm.LLMError):
        llm.grade_answer("Why?", ["because"], "A passage.", "It happens.")


def test_quiz_api_reopening_moves_past_an_unanswered_passage(client, monkeypatch):
    seen: list[str] = []

    def generate(title, heading, passage):
        seen.append(passage)
        return fake_question(title, heading, passage)

    monkeypatch.setattr(server, "generate_question", generate)
    start_quiz(client)
    assert seen[0].startswith("Passage 3")

    start_quiz(client)
    assert seen[1].startswith("Passage 2")


def test_quiz_generate_prompt_asks_for_reasoning_not_fact_recall(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    captured: dict[str, str] = {}

    def call(config, prompt, system, max_tokens):
        captured["prompt"] = prompt
        captured["system"] = system
        return (
            '{"question": "Why does the path length change the cost?", '
            '"kind": "explain", "expected": ["x"], "hints": ["y"]}'
        )

    monkeypatch.setattr(llm, "_call_llm", call)
    llm.generate_question("Title", "Heading", "A passage.")
    text = f"{captured['system']} {captured['prompt']}".lower()
    assert "reasoning" in text
    assert "flashcard" in text
    assert "a definition is fair game only when" in text


def test_quiz_grade_prompt_does_not_accept_a_fact_as_reasoning(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    captured: dict[str, str] = {}

    def call(config, prompt, system, max_tokens):
        captured["prompt"] = prompt
        captured["system"] = system
        return '{"satisfactory": true, "feedback": "ok"}'

    monkeypatch.setattr(llm, "_call_llm", call)
    llm.grade_answer("Why?", ["because"], "A passage.", "Something.")
    text = f"{captured['system']} {captured['prompt']}".lower()
    assert "expected reasoning" in text
    assert "remembered fact" in text or "correct fact" in text


def test_quiz_generate_falls_back_to_the_explain_kind(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        llm,
        "_call_llm",
        lambda *args, **kwargs: (
            '{"question": "Why?", "kind": "trivia", "expected": ["x"], "hints": ["y"]}'
        ),
    )
    assert llm.generate_question("T", "H", "P")["kind"] == "explain"
