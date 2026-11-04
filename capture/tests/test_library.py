"""Tests for the reading library: store, backfill, API, and Telegram."""

from __future__ import annotations

import pytest

from capture import backfill, library, server, store, telegram


@pytest.fixture()
def app(tmp_path):
    application = server.create_app(str(tmp_path / "cards.db"))
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    with app.test_client() as test_client:
        yield test_client


EXPORT_HTML = """<html><body>
<div class="message default clearfix" id="message10">
  <div class="body">
    <div class="date" title="28.06.2022 07:45:56 UTC-08:00"></div>
    <div class="text">Read <a href="https://example.com/attention?utm_source=tg">Attention explained</a> today</div>
  </div>
</div>
<div class="message default clearfix" id="message11">
  <div class="body">
    <div class="date" title="28.06.2022 07:50:00 UTC-08:00"></div>
    <div class="text">https://youtu.be/abc123</div>
  </div>
</div>
<div class="message default clearfix" id="message12">
  <div class="body">
    <div class="date" title="28.06.2022 07:51:00 UTC-08:00"></div>
    <div class="text">Carmen</div>
  </div>
</div>
<div class="message default clearfix" id="message13">
  <div class="body">
    <div class="date" title="28.06.2022 07:52:00 UTC-08:00"></div>
    <div class="text">Two <a href="https://a.example/1">One</a> <a href="https://b.example/2">Two</a></div>
  </div>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def test_upsert_keeps_user_status_and_fills_empty_fields(app):
    with app.app_context():
        first = store.upsert_reading_item(
            {"url": "https://example.com/a", "url_key": "https://example.com/a"}
        )
        assert first["status"] == "unread"
        store.update_reading_item(first["id"], {"status": "done"})

        again = store.upsert_reading_item(
            {
                "url": "https://example.com/a",
                "url_key": "https://example.com/a",
                "title": "A story",
                "description": "Saved from Telegram",
            }
        )
        assert again["id"] == first["id"]
        assert again["status"] == "done"
        assert again["title"] == "A story"
        assert again["description"] == "Saved from Telegram"


def test_counts_sites_and_filters(app):
    with app.app_context():
        for key, site, status in [
            ("https://a.example/1", "a.example", "unread"),
            ("https://a.example/2", "a.example", "done"),
            ("https://b.example/1", "b.example", "unread"),
        ]:
            item = store.upsert_reading_item(
                {"url": key, "url_key": key, "site": site, "title": key}
            )
            store.update_reading_item(item["id"], {"status": status})

        assert store.reading_counts() == {
            "unread": 2,
            "reading": 0,
            "done": 1,
            "dropped": 0,
            "total": 3,
        }
        assert store.reading_sites()[0] == {"site": "a.example", "count": 2}
        assert len(store.list_reading_items(status="done")) == 1
        assert len(store.list_reading_items(site="b.example")) == 1
        assert store.count_reading_items(query="b.example") == 1


def test_update_rejects_unknown_status(app):
    with app.app_context():
        item = store.upsert_reading_item(
            {"url": "https://example.com/x", "url_key": "https://example.com/x"}
        )
        with pytest.raises(ValueError):
            store.update_reading_item(item["id"], {"status": "archived"})


# ---------------------------------------------------------------------------
# Export backfill
# ---------------------------------------------------------------------------

def test_backfill_parses_links_titles_and_notes(app, tmp_path):
    html_path = tmp_path / "messages.html"
    html_path.write_text(EXPORT_HTML, encoding="utf-8")

    with app.app_context():
        counts = backfill.import_export(str(html_path))
        assert counts == {"seen": 5, "added": 5, "total": 5}

        items = {item["url_key"]: item for item in store.list_reading_items()}
        attention = items["https://example.com/attention"]
        assert attention["title"] == "Attention explained"
        assert attention["site"] == "example.com"
        assert attention["source"] == "export"
        assert attention["saved_at"] == "2022-06-28T15:45:56Z"

        video = items["https://youtu.be/abc123"]
        assert video["title"] is None
        assert video["description"] is None
        assert video["site"] == "youtu.be"

        assert items["export:12"]["url"] is None
        assert items["export:12"]["description"] == "Carmen"
        assert items["https://a.example/1"]["title"] == "One"
        assert items["https://b.example/2"]["title"] == "Two"

        assert backfill.import_export(str(html_path))["added"] == 0


def test_backfill_keeps_status_on_a_second_run(app, tmp_path):
    html_path = tmp_path / "messages.html"
    html_path.write_text(EXPORT_HTML, encoding="utf-8")

    with app.app_context():
        backfill.import_export(str(html_path))
        item = store.get_reading_item_by_key("https://example.com/attention")
        store.update_reading_item(item["id"], {"status": "done"})

        backfill.import_export(str(html_path))

        again = store.get_reading_item_by_key("https://example.com/attention")
        assert again["status"] == "done"


# ---------------------------------------------------------------------------
# Library API
# ---------------------------------------------------------------------------

def test_library_api_lists_filters_and_counts(app, client):
    with app.app_context():
        for key, site, status in [
            ("https://a.example/1", "a.example", "unread"),
            ("https://b.example/1", "b.example", "done"),
        ]:
            item = store.upsert_reading_item(
                {"url": key, "url_key": key, "site": site, "title": key}
            )
            store.update_reading_item(item["id"], {"status": status})

    data = client.get("/api/library").get_json()
    assert data["total"] == 2
    assert data["count"] == 2
    assert data["counts"]["done"] == 1
    assert {row["site"] for row in data["sites"]} == {"a.example", "b.example"}

    assert client.get("/api/library?status=done").get_json()["count"] == 1
    assert client.get("/api/library?q=b.example").get_json()["count"] == 1
    assert client.get("/api/library?site=a.example").get_json()["count"] == 1


def test_library_api_paginates(app, client):
    with app.app_context():
        for index in range(5):
            key = f"https://e.example/{index}"
            store.upsert_reading_item(
                {
                    "url": key,
                    "url_key": key,
                    "saved_at": f"2026-01-0{index + 1}T00:00:00Z",
                }
            )

    first = client.get("/api/library?limit=2").get_json()
    assert first["count"] == 2
    assert first["total"] == 5
    assert first["has_more"] is True

    last = client.get("/api/library?limit=2&offset=4").get_json()
    assert last["count"] == 1
    assert last["has_more"] is False
    assert last["offset"] == 4


def test_library_api_add_stores_canonical_link(client):
    response = client.post(
        "/api/library",
        json={"url": "https://Example.com/watch?v=1&utm_source=x"},
    )
    assert response.status_code == 201
    item = response.get_json()["item"]
    assert item["url"] == "https://example.com/watch?v=1"
    assert item["site"] == "example.com"
    assert item["source"] == "manual"
    assert item["status"] == "unread"

    again = client.post(
        "/api/library", json={"url": "https://example.com/watch?v=1"}
    )
    assert again.get_json()["item"]["id"] == item["id"]

    assert client.post("/api/library", json={"url": "not a url"}).status_code == 400
    assert client.post("/api/library", json={}).status_code == 400


def test_library_api_updates_status_and_deletes(app, client):
    with app.app_context():
        item = store.upsert_reading_item(
            {"url": "https://example.com/a", "url_key": "https://example.com/a"}
        )
    item_id = item["id"]

    response = client.patch(
        f"/api/library/{item_id}", json={"status": "reading"}
    )
    assert response.status_code == 200
    assert response.get_json()["item"]["status"] == "reading"

    assert client.patch(
        f"/api/library/{item_id}", json={"status": "nope"}
    ).status_code == 400
    assert client.patch(
        "/api/library/9999", json={"status": "done"}
    ).status_code == 404

    assert client.delete(f"/api/library/{item_id}").status_code == 200
    assert client.delete(f"/api/library/{item_id}").status_code == 404


def test_library_page_renders(client):
    response = client.get("/library")
    assert response.status_code == 200
    assert b"Reading list" in response.data


# ---------------------------------------------------------------------------
# Done list
# ---------------------------------------------------------------------------

def done_config(done_chat_id="-200"):
    return {
        "enabled": True,
        "token": "token",
        "chat_ids": (),
        "done_chat_id": done_chat_id,
        "timeout": 1,
    }


def test_done_forwards_then_deletes(app, client, monkeypatch):
    with app.app_context():
        item = store.upsert_reading_item(
            {
                "url": "https://example.com/a",
                "url_key": "https://example.com/a",
                "chat_id": "-100",
                "message_id": 5,
            }
        )
    calls = {}

    def fake_forward(token, done_chat, source_chat, message_id):
        calls["forward"] = (token, done_chat, source_chat, message_id)
        return {"message_id": 77}

    def fake_delete(token, chat_id, message_id):
        calls["delete"] = (token, chat_id, message_id)

    monkeypatch.setattr(telegram, "get_telegram_config", done_config)
    monkeypatch.setattr(telegram, "forward_message", fake_forward)
    monkeypatch.setattr(telegram, "delete_message", fake_delete)

    response = client.patch(f"/api/library/{item['id']}", json={"status": "done"})

    assert response.status_code == 200
    body = response.get_json()
    assert body["archive"] == {"ok": True, "forwarded": True, "deleted": True}
    assert calls["forward"] == ("token", "-200", "-100", 5)
    assert calls["delete"] == ("token", "-100", 5)
    assert body["item"]["archived_at"]
    assert body["item"]["archive_message_id"] == 77
    assert body["item"]["status"] == "done"


def test_done_reverts_when_forward_fails(app, client, monkeypatch):
    with app.app_context():
        item = store.upsert_reading_item(
            {
                "url": "https://example.com/b",
                "url_key": "https://example.com/b",
                "chat_id": "-100",
                "message_id": 6,
            }
        )

    def boom(*_args):
        raise telegram.TelegramError("not enough rights")

    monkeypatch.setattr(telegram, "get_telegram_config", done_config)
    monkeypatch.setattr(telegram, "forward_message", boom)

    response = client.patch(f"/api/library/{item['id']}", json={"status": "done"})

    assert response.status_code == 502
    assert "not enough rights" in response.get_json()["error"]
    with app.app_context():
        assert store.get_reading_item(item["id"])["status"] == "unread"


def test_done_without_done_chat_stays_done(app, client, monkeypatch):
    with app.app_context():
        item = store.upsert_reading_item(
            {
                "url": "https://example.com/c",
                "url_key": "https://example.com/c",
                "chat_id": "-100",
                "message_id": 7,
            }
        )

    monkeypatch.setattr(telegram, "get_telegram_config", lambda: done_config(""))

    response = client.patch(f"/api/library/{item['id']}", json={"status": "done"})

    assert response.status_code == 200
    body = response.get_json()
    assert body["item"]["status"] == "done"
    assert body["archive"]["skipped"] == "TELEGRAM_DONE_CHAT_ID is not set"


def archived_item(app, key="https://example.com/u", *, delete_failed=False):
    with app.app_context():
        item = store.upsert_reading_item(
            {
                "url": key,
                "url_key": key,
                "chat_id": "-100",
                "message_id": 5,
            }
        )
        store.update_reading_item(item["id"], {"status": "done"})
        store.mark_reading_archived(item["id"], 77)
        if delete_failed:
            store.set_reading_archive_error(item["id"], "refused")
        return store.get_reading_item(item["id"])


def test_undo_reposts_and_removes_forward(app, client, monkeypatch):
    item = archived_item(app)
    calls = {}

    def fake_send(token, chat_id, text):
        calls["send"] = (token, chat_id, text)
        return {"message_id": 99}

    def fake_delete(token, chat_id, message_id):
        calls["delete"] = (token, chat_id, message_id)

    monkeypatch.setattr(telegram, "get_telegram_config", done_config)
    monkeypatch.setattr(telegram, "send_message", fake_send)
    monkeypatch.setattr(telegram, "delete_message", fake_delete)

    response = client.post(f"/api/library/{item['id']}/undo")

    assert response.status_code == 200
    body = response.get_json()
    assert body["undo"] == {
        "ok": True,
        "repost": True,
        "unforwarded": True,
        "message_id": 99,
    }
    assert calls["send"] == ("token", "-100", "https://example.com/u")
    assert calls["delete"] == ("token", "-200", 77)
    assert body["item"]["status"] == "unread"
    assert body["item"]["message_id"] == 99
    assert body["item"]["archived_at"] is None
    assert body["item"]["archive_message_id"] is None


def test_undo_skips_repost_when_the_source_survived(app, client, monkeypatch):
    item = archived_item(app, delete_failed=True)
    calls = {}

    def fake_send(*_args):
        calls["send"] = True
        return {"message_id": 99}

    def fake_delete(token, chat_id, message_id):
        calls["delete"] = (token, chat_id, message_id)

    monkeypatch.setattr(telegram, "get_telegram_config", done_config)
    monkeypatch.setattr(telegram, "send_message", fake_send)
    monkeypatch.setattr(telegram, "delete_message", fake_delete)

    response = client.post(f"/api/library/{item['id']}/undo")

    assert response.status_code == 200
    assert "send" not in calls
    assert calls["delete"] == ("token", "-200", 77)
    assert response.get_json()["item"]["message_id"] == 5


def test_undo_unarchived_done_only_resets_status(app, client):
    with app.app_context():
        item = store.upsert_reading_item(
            {"url": "https://example.com/v", "url_key": "https://example.com/v"}
        )
        store.update_reading_item(item["id"], {"status": "done"})

    response = client.post(f"/api/library/{item['id']}/undo")

    assert response.status_code == 200
    body = response.get_json()
    assert body["undo"] == {"ok": True, "skipped": "not archived"}
    assert body["item"]["status"] == "unread"


def test_undo_rejects_items_that_are_not_done(app, client):
    with app.app_context():
        item = store.upsert_reading_item(
            {"url": "https://example.com/w", "url_key": "https://example.com/w"}
        )

    response = client.post(f"/api/library/{item['id']}/undo")

    assert response.status_code == 409


def test_undo_send_failure_keeps_the_item_done(app, client, monkeypatch):
    item = archived_item(app)

    def boom(*_args):
        raise telegram.TelegramError("not enough rights")

    monkeypatch.setattr(telegram, "get_telegram_config", done_config)
    monkeypatch.setattr(telegram, "send_message", boom)

    response = client.post(f"/api/library/{item['id']}/undo")

    assert response.status_code == 502
    with app.app_context():
        stored = store.get_reading_item(item["id"])
        assert stored["status"] == "done"
        assert stored["archived_at"] is not None


# ---------------------------------------------------------------------------
# Telegram ingestion
# ---------------------------------------------------------------------------

def make_update(
    text,
    *,
    entities=None,
    preview=None,
    chat_id=-100,
    message_id=7,
    update_id=500,
    date=1700000000,
):
    message = {
        "message_id": message_id,
        "date": date,
        "chat": {"id": chat_id},
        "text": text,
    }
    if entities:
        message["entities"] = entities
    if preview:
        message["link_preview_options"] = preview
    return {"update_id": update_id, "message": message}


def test_parse_text_link_uses_anchor_as_title():
    text = "Attention explained https://example.com/attention"
    update = make_update(
        text,
        entities=[
            {
                "type": "text_link",
                "offset": 0,
                "length": 19,
                "url": "https://example.com/attention?utm_source=tg",
            }
        ],
    )

    items = telegram.parse_update(update)

    assert len(items) == 1
    assert items[0]["url"] == "https://example.com/attention"
    assert items[0]["title"] == "Attention explained"


def test_parse_uses_preview_for_bare_link():
    update = make_update(
        "https://example.com/a",
        preview={
            "url": "https://example.com/a",
            "title": "A Story",
            "description": "About A",
            "site_name": "Example",
        },
    )

    item = telegram.parse_update(update)[0]

    assert item["title"] == "A Story"
    assert item["site"] == "Example"
    assert item["description"] == "About A"


def test_parse_note_without_link():
    item = telegram.parse_update(make_update("Carmen"))[0]

    assert item["url"] is None
    assert item["url_key"] == "tg:-100:7"
    assert item["description"] == "Carmen"


def test_parse_multiple_links_make_separate_rows():
    items = telegram.parse_update(
        make_update("https://a.example/1 https://b.example/2")
    )

    assert {item["url"] for item in items} == {
        "https://a.example/1",
        "https://b.example/2",
    }


def test_entity_offsets_use_utf16_units():
    text = "🔥 https://example.com/a"
    items = telegram.parse_update(
        make_update(
            text,
            entities=[{"type": "url", "offset": 3, "length": 21}],
        )
    )

    assert items[0]["url"] == "https://example.com/a"


def test_ingest_is_idempotent_and_advances_offset(app):
    update = make_update(
        "Read https://example.com/x",
        entities=[{"type": "url", "offset": 5, "length": 21}],
        update_id=900,
    )

    assert telegram.ingest_updates(app, [update]) == {"items": 1, "offset": 901}

    with app.app_context():
        item = store.list_reading_items()[0]
        store.update_reading_item(item["id"], {"status": "done"})
        assert store.get_state("telegram_offset") == "901"

    assert telegram.ingest_updates(app, [update])["items"] == 1

    with app.app_context():
        items = store.list_reading_items()
        assert len(items) == 1
        assert items[0]["status"] == "done"
        assert store.get_state("telegram_offset") == "901"


def test_ingest_filters_other_chats_and_still_advances(app):
    update = make_update("https://example.com/x", chat_id=-200, update_id=42)

    result = telegram.ingest_updates(app, [update], chat_ids=("-100",))

    assert result["items"] == 0
    with app.app_context():
        assert store.reading_counts()["total"] == 0
        assert store.get_state("telegram_offset") == "43"
