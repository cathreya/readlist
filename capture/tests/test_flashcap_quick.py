"""Tests for the out-of-page quick-capture helper."""

from __future__ import annotations

import urllib.parse

import pytest

from capture.flashcap_quick import MAX_TEXT, build_url


def _params(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


def test_builds_quick_url_with_encoded_text():
    url = build_url('He said "no" & left', base="http://127.0.0.1:8765")
    parts = urllib.parse.urlsplit(url)

    assert parts.path == "/quick"
    assert _params(url)["text"] == 'He said "no" & left'
    assert " " not in url


def test_optional_fields_only_when_present():
    url = build_url("passage", base="http://x", title="Paper", url="", hint="")
    params = _params(url)

    assert params == {"text": "passage", "title": "Paper"}


def test_truncates_long_passages():
    url = build_url("x" * (MAX_TEXT + 500), base="http://x")

    assert len(_params(url)["text"]) == MAX_TEXT


def test_rejects_empty_text():
    with pytest.raises(ValueError):
        build_url("   \n  ")


def test_strips_trailing_slash_from_base():
    assert build_url("a", base="http://x:8765/").startswith("http://x:8765/quick?")
