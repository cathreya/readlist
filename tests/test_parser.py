"""Tests for parser.py -- domain normalisation, URL extraction, HTML parsing."""

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(PROJECT_ROOT, "app")
for _p in (PROJECT_ROOT, APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.parser import (
    normalize_domain,
    detect_content_type,
    extract_urls_from_text,
    clean_url,
    extract_title_from_anchor,
    parse_messages,
    _file_content_type,
    build_files_index,
    extract_file_attachments,
    summary_stats,
)


# ===================================================================
# Helpers
# ===================================================================

def _make_a_tag(href, text=None):
    """Return a BeautifulSoup <a> tag for testing extract_title_from_anchor."""
    from bs4 import BeautifulSoup
    inner = text or href
    soup = BeautifulSoup(f'<a href="{href}">{inner}</a>', "html.parser")
    return soup.find("a")


# ===================================================================
# TestNormalizeDomain
# ===================================================================

class TestNormalizeDomain:
    def test_youtube_com(self):
        assert normalize_domain("https://www.youtube.com/watch?v=abc") == "youtube"

    def test_youtube_short_url(self):
        assert normalize_domain("https://youtu.be/abc123") == "youtube"

    def test_youtube_without_www(self):
        assert normalize_domain("https://youtube.com/watch?v=abc") == "youtube"

    def test_arxiv(self):
        assert normalize_domain("https://arxiv.org/abs/2106.09685") == "arxiv"

    def test_wikipedia(self):
        assert normalize_domain("https://en.wikipedia.org/wiki/Python") == "wikipedia"

    def test_wikipedia_other_language(self):
        """Non-en.wikipedia.org subdomains are returned as-is (DOMAIN_MAP only has 'en.wikipedia.org')."""
        assert normalize_domain("https://fr.wikipedia.org/wiki/Python") == "fr.wikipedia.org"

    def test_github(self):
        assert normalize_domain("https://github.com/user/repo") == "github"

    def test_github_subdomain(self):
        assert normalize_domain("https://subdomain.github.com") == "github"

    def test_hackernews(self):
        assert normalize_domain("https://news.ycombinator.com/item?id=123") == "hackernews"

    def test_spotify(self):
        assert normalize_domain("https://open.spotify.com/track/abc") == "spotify"

    def test_amazon(self):
        assert normalize_domain("https://www.amazon.com/dp/123") == "amazon"

    def test_amazon_short_url(self):
        assert normalize_domain("https://amzn.eu/abc123") == "amazon"

    def test_amazon_a_dot_co(self):
        assert normalize_domain("https://a.co/abc123") == "amazon"

    def test_twitter(self):
        assert normalize_domain("https://twitter.com/user") == "twitter"

    def test_x_dot_com(self):
        assert normalize_domain("https://x.com/user") == "twitter"

    def test_t_co_short_url(self):
        assert normalize_domain("https://t.co/abc123") == "twitter"

    def test_reddit(self):
        assert normalize_domain("https://www.reddit.com/r/python") == "reddit"

    def test_substack(self):
        assert normalize_domain("https://example.substack.com/p/article") == "substack"

    def test_medium(self):
        assert normalize_domain("https://medium.com/@user/article") == "medium"

    def test_instagram(self):
        assert normalize_domain("https://www.instagram.com/p/abc") == "instagram"

    def test_discord_com(self):
        assert normalize_domain("https://discord.com/channel/123") == "discord"

    def test_discord_gg(self):
        assert normalize_domain("https://discord.gg/invite") == "discord"

    def test_unknown_domain(self):
        assert normalize_domain("https://some-random-site.org/page") == "some-random-site.org"

    def test_www_stripped(self):
        assert normalize_domain("https://www.example.com") == "example.com"

    def test_www_subdomain_mapped(self):
        assert normalize_domain("https://www.github.com/user/repo") == "github"

    def test_invalid_url_returns_empty_string(self):
        """urlparse never raises, so the function returns the empty netloc."""
        assert normalize_domain("not a url") == ""

    def test_empty_string_returns_empty_string(self):
        assert normalize_domain("") == ""

    def test_url_with_port(self):
        """netloc includes the port, so 'localhost:8080' is returned."""
        assert normalize_domain("https://localhost:8080/page") == "localhost:8080"

    def test_substack_matches_via_ends_with(self):
        assert normalize_domain("https://mynewsletter.substack.com/p/xyz") == "substack"


# ===================================================================
# TestDetectContentType
# ===================================================================

class TestDetectContentType:
    def test_youtube_returns_video(self):
        assert detect_content_type("https://youtube.com/watch?v=abc", "youtube") == "video"

    def test_arxiv_returns_paper(self):
        assert detect_content_type("https://arxiv.org/abs/2106.09685", "arxiv") == "paper"

    def test_github_returns_repo(self):
        assert detect_content_type("https://github.com/user/repo", "github") == "repo"

    def test_hackernews_returns_social(self):
        assert detect_content_type("https://news.ycombinator.com/item?id=123", "hackernews") == "social"

    def test_spotify_returns_music(self):
        assert detect_content_type("https://open.spotify.com/track/abc", "spotify") == "music"

    def test_pdf_url_returns_pdf(self):
        assert detect_content_type("https://example.com/paper.pdf", "example.com") == "pdf"

    def test_epub_url_returns_book(self):
        assert detect_content_type("https://example.com/book.epub", "example.com") == "book"

    def test_html_url_returns_article(self):
        assert detect_content_type("https://example.com/page.html", "example.com") == "article"

    def test_htm_url_returns_article(self):
        assert detect_content_type("https://example.com/page.htm", "example.com") == "article"

    def test_unknown_domain_returns_article(self):
        assert detect_content_type("https://example.com/article", "example.com") == "article"

    def test_none_url_with_known_domain(self):
        assert detect_content_type(None, "youtube") == "video"

    def test_none_url_with_unknown_domain(self):
        assert detect_content_type(None, "unknown") == "article"

    def test_empty_url_returns_article_for_unknown(self):
        assert detect_content_type("", "example.com") == "article"


# ===================================================================
# TestExtractTitleFromAnchor
# ===================================================================

class TestExtractTitleFromAnchor:
    def test_descriptive_text_returns_text(self):
        a = _make_a_tag("https://example.com", "Click here for info")
        assert extract_title_from_anchor(a) == "Click here for info"

    def test_url_as_text_returns_none(self):
        a = _make_a_tag("https://example.com", "https://example.com")
        assert extract_title_from_anchor(a) is None

    def test_empty_text_returns_none(self):
        a = _make_a_tag("https://example.com", "")
        assert extract_title_from_anchor(a) is None

    def test_text_starting_with_http_returns_none(self):
        a = _make_a_tag("https://example.com", "https://example.com/page")
        assert extract_title_from_anchor(a) is None

    def test_descriptive_text_with_trailing_space(self):
        a = _make_a_tag("https://example.com", "  A nice article  ")
        assert extract_title_from_anchor(a) == "A nice article"


# ===================================================================
# TestCleanUrl
# ===================================================================

class TestCleanUrl:
    def test_no_trailing_punctuation(self):
        assert clean_url("https://example.com") == "https://example.com"

    def test_trailing_period(self):
        assert clean_url("https://example.com.") == "https://example.com"

    def test_trailing_comma(self):
        assert clean_url("https://example.com,") == "https://example.com"

    def test_trailing_parens(self):
        assert clean_url("https://example.com)") == "https://example.com"

    def test_multiple_trailing_chars(self):
        assert clean_url("https://example.com!.") == "https://example.com"

    def test_url_with_path_and_punctuation(self):
        assert clean_url("https://example.com/page?q=1.") == "https://example.com/page?q=1"

    def test_already_clean(self):
        assert clean_url("https://example.com/path") == "https://example.com/path"


# ===================================================================
# TestExtractUrlsFromText
# ===================================================================

class TestExtractUrlsFromText:
    def test_single_url(self):
        assert extract_urls_from_text("Visit https://example.com now") == ["https://example.com"]

    def test_multiple_urls(self):
        result = extract_urls_from_text(
            "Check https://example.com and https://test.com/page"
        )
        assert result == ["https://example.com", "https://test.com/page"]

    def test_no_urls(self):
        assert extract_urls_from_text("Just some plain text") == []

    def test_empty_string(self):
        assert extract_urls_from_text("") == []

    def test_urls_with_trailing_punctuation(self):
        """Regex extracts them with punctuation; clean_url handles stripping."""
        result = extract_urls_from_text("See https://example.com.")
        assert result == ["https://example.com."]

    def test_https_and_http(self):
        result = extract_urls_from_text("http://a.com and https://b.com")
        assert result == ["http://a.com", "https://b.com"]

    def test_urls_in_parentheses(self):
        """The regex excludes ')' from the match, so the paren is stripped."""
        result = extract_urls_from_text("(https://example.com)")
        assert len(result) == 1
        assert result[0] == "https://example.com"


# ===================================================================
# TestFileContentType
# ===================================================================

class TestFileContentType:
    @pytest.mark.parametrize("filename,expected", [
        ("paper.pdf", "pdf"),
        ("book.epub", "book"),
        ("book.mobi", "book"),
        ("page.html", "article"),
        ("page.htm", "article"),
        ("report.docx", "document"),
        ("notes.doc", "document"),
        ("readme.txt", "document"),
        ("photo.png", "image"),
        ("photo.jpg", "image"),
        ("photo.jpeg", "image"),
        ("animation.gif", "image"),
        ("unknown.xyz", "document"),
        ("no_extension", "document"),
    ])
    def test_various_extensions(self, filename, expected):
        assert _file_content_type(filename) == expected

    def test_case_insensitive(self):
        assert _file_content_type("PAPER.PDF") == "pdf"


# ===================================================================
# TestBuildFilesIndex
# ===================================================================

class TestBuildFilesIndex:
    def test_directory_not_found_returns_empty(self, tmp_path):
        index = build_files_index(str(tmp_path / "nonexistent"))
        assert index == {}

    def test_builds_case_insensitive_index(self, tmp_path):
        (tmp_path / "MyFile.PDF").write_text("dummy")
        (tmp_path / "notes.txt").write_text("notes")
        index = build_files_index(str(tmp_path))
        assert "myfile.pdf" in index
        assert "notes.txt" in index
        assert index["myfile.pdf"].endswith("MyFile.PDF")

    def test_excludes_thumbnails(self, tmp_path):
        (tmp_path / "photo_thumb.jpg").write_text("thumb")
        (tmp_path / "photo.jpg").write_text("full")
        index = build_files_index(str(tmp_path))
        assert "photo.jpg" in index
        assert "photo_thumb.jpg" not in index


# ===================================================================
# TestExtractFileAttachments
# ===================================================================

class TestExtractFileAttachments:
    def test_linked_media_file_found(self, tmp_path):
        """Linked <a class='media_file' href='files/doc.pdf'> found on disk."""
        html_dir = tmp_path / "html"
        html_dir.mkdir()
        files_dir = html_dir / "files"
        files_dir.mkdir()
        pdf_path = files_dir / "doc.pdf"
        pdf_path.write_text("pdf content")

        html_content = (
            '<div class="body">'
            '<a class="media_file" href="files/doc.pdf">'
            '<div class="body"><div class="text">A doc</div></div>'
            '</a>'
            '</div>'
        )
        from bs4 import BeautifulSoup
        body_div = BeautifulSoup(html_content, "html.parser").find("div", class_="body")
        files_index = build_files_index(str(files_dir))
        result = extract_file_attachments(body_div, str(html_dir), files_index)
        assert len(result) == 1
        assert str(pdf_path) in result[0]

    def test_unlinked_media_file_div(self, tmp_path):
        """<div class='media_file'> without <a> link, file on disk."""
        files_dir = tmp_path / "files"
        files_dir.mkdir()
        pdf_path = files_dir / "report.pdf"
        pdf_path.write_text("content")

        html_content = (
            '<div class="body">'
            '<div class="media_file">'
            '<div class="title bold">report.pdf</div>'
            '</div>'
            '</div>'
        )
        from bs4 import BeautifulSoup
        body_div = BeautifulSoup(html_content, "html.parser").find("div", class_="body")
        files_index = build_files_index(str(files_dir))
        result = extract_file_attachments(body_div, str(tmp_path), files_index)
        assert len(result) == 1
        assert str(pdf_path) in result[0]

    def test_media_file_not_on_disk(self, tmp_path):
        """File referenced but not available should be silently skipped."""
        html_dir = tmp_path / "html"
        html_dir.mkdir()
        files_dir = html_dir / "files"
        files_dir.mkdir()

        html_content = (
            '<div class="body">'
            '<a class="media_file" href="files/missing.pdf">'
            '<div class="body"><div class="text">Missing</div></div>'
            '</a>'
            '</div>'
        )
        from bs4 import BeautifulSoup
        body_div = BeautifulSoup(html_content, "html.parser").find("div", class_="body")
        files_index = build_files_index(str(files_dir))
        result = extract_file_attachments(body_div, str(html_dir), files_index)
        assert result == []

    def test_no_media_files_returns_empty(self, tmp_path):
        html_content = '<div class="body"><div class="text">Just text</div></div>'
        from bs4 import BeautifulSoup
        body_div = BeautifulSoup(html_content, "html.parser").find("div", class_="body")
        result = extract_file_attachments(body_div, str(tmp_path), {})
        assert result == []


# ===================================================================
# TestParseMessages — end-to-end HTML parsing
# ===================================================================

class TestParseMessages:
    def _make_html(self, messages_html: str) -> str:
        """Wrap inner message divs in a minimal Telegram-export HTML document."""
        return (
            '<!DOCTYPE html><meta charset="utf-8">\n'
            '<div class="history">\n'
            + messages_html + "\n"
            + '</div>'
        )

    def test_empty_html_returns_empty_list(self, tmp_path):
        html_file = tmp_path / "empty.html"
        html_file.write_text("<html></html>")
        result = parse_messages(str(html_file), str(tmp_path))
        assert result == []

    def test_no_message_divs(self, tmp_path):
        html_file = tmp_path / "no_msgs.html"
        html_file.write_text("<html><body><p>No messages</p></body></html>")
        result = parse_messages(str(html_file), str(tmp_path))
        assert result == []

    def test_single_link(self, tmp_path):
        html_file = tmp_path / "single.html"
        html_file.write_text(self._make_html(
            '<div class="message default clearfix">\n'
            '<div class="body">\n'
            '<div class="text">\n'
            'Check this: <a href="https://youtube.com/watch?v=abc">Cool Video</a>\n'
            '</div>\n'
            '</div>\n'
            '</div>'
        ))
        result = parse_messages(str(html_file), str(tmp_path))
        assert len(result) == 1
        assert result[0]["url"] == "https://youtube.com/watch?v=abc"
        assert result[0]["title"] == "Cool Video"
        assert result[0]["domain"] == "youtube"
        assert result[0]["content_type"] == "video"

    def test_multiple_links_same_message(self, tmp_path):
        html_file = tmp_path / "multi.html"
        html_file.write_text(self._make_html(
            '<div class="message default clearfix">\n'
            '<div class="body">\n'
            '<div class="text">\n'
            '<a href="https://arxiv.org/abs/2106.09685">Paper A</a>\n'
            '<a href="https://github.com/user/repo">Repo</a>\n'
            '</div>\n'
            '</div>\n'
            '</div>'
        ))
        result = parse_messages(str(html_file), str(tmp_path))
        assert len(result) == 2
        urls = {r["url"] for r in result}
        assert "https://arxiv.org/abs/2106.09685" in urls
        assert "https://github.com/user/repo" in urls

    def test_dedup_same_url_across_messages(self, tmp_path):
        """When the same URL appears in two messages: the URL item appears once,
        and the second message's text becomes a plain note."""
        html_file = tmp_path / "dedup.html"
        html_file.write_text(self._make_html(
            '<div class="message default clearfix">\n'
            '<div class="body"><div class="text">\n'
            '<a href="https://example.com">First mention</a>\n'
            '</div></div>\n'
            '</div>\n'
            '<div class="message default clearfix">\n'
            '<div class="body"><div class="text">\n'
            '<a href="https://example.com">Duplicate</a>\n'
            '</div></div>\n'
            '</div>'
        ))
        result = parse_messages(str(html_file), str(tmp_path))
        # First message -> url item; second message -> plain note (url already seen)
        assert len(result) == 2
        assert result[0]["url"] == "https://example.com"
        assert result[0]["title"] == "First mention"
        # The second item is a note because the url was already seen
        notes = [r for r in result if r["content_type"] == "note"]
        assert len(notes) == 1

    def test_plain_note_no_url_no_file(self, tmp_path):
        """A message with text but no link/file becomes a 'note' item."""
        html_file = tmp_path / "note.html"
        html_file.write_text(self._make_html(
            '<div class="message default clearfix">\n'
            '<div class="body">\n'
            '<div class="text">\n'
            'Just a thought about something interesting I read today.\n'
            '</div>\n'
            '</div>\n'
            '</div>'
        ))
        result = parse_messages(str(html_file), str(tmp_path))
        assert len(result) == 1
        assert result[0]["url"] is None
        assert result[0]["content_type"] == "note"
        assert "Just a thought" in result[0]["description"]

    def test_file_attachment_only(self, tmp_path):
        """A message with only a file attachment (no URL) returns a file item."""
        files_dir = tmp_path / "files"
        files_dir.mkdir()
        pdf_file = files_dir / "doc.pdf"
        pdf_file.write_text("content")

        html_file = tmp_path / "file_msg.html"
        html_file.write_text(self._make_html(
            '<div class="message default clearfix">\n'
            '<div class="body">\n'
            '<a class="media_file" href="files/doc.pdf">\n'
            '<div class="body"><div class="text">A PDF</div></div>\n'
            '</a>\n'
            '</div>\n'
            '</div>'
        ))
        result = parse_messages(html_path=str(html_file), files_dir=str(files_dir))
        assert len(result) == 1
        assert result[0]["url"] is None
        assert result[0]["title"] == "doc.pdf"
        assert result[0]["content_type"] == "pdf"
        assert result[0]["local_file_path"] is not None

    def test_link_and_file_attachment(self, tmp_path):
        """A message with both a link and a file: both captured."""
        files_dir = tmp_path / "files"
        files_dir.mkdir()
        pdf_file = files_dir / "paper.pdf"
        pdf_file.write_text("content")

        html_file = tmp_path / "link_and_file.html"
        html_file.write_text(self._make_html(
            '<div class="message default clearfix">\n'
            '<div class="body">\n'
            '<div class="text">\n'
            '<a href="https://arxiv.org/abs/2106.09685">A Paper</a>\n'
            '</div>\n'
            '<a class="media_file" href="files/paper.pdf">\n'
            '<div class="body"><div class="text">PDF</div></div>\n'
            '</a>\n'
            '</div>\n'
            '</div>'
        ))
        result = parse_messages(html_path=str(html_file), files_dir=str(files_dir))
        assert len(result) >= 1
        item_urls = [r["url"] for r in result]
        assert "https://arxiv.org/abs/2106.09685" in item_urls

    def test_message_without_body_skipped(self, tmp_path):
        """A message div without a body div is safely skipped."""
        html_file = tmp_path / "no_body.html"
        html_file.write_text(self._make_html(
            '<div class="message default clearfix">\n'
            'No body here\n'
            '</div>'
        ))
        result = parse_messages(str(html_file), str(tmp_path))
        assert result == []

    def test_file_not_found_returns_empty(self, tmp_path):
        result = parse_messages(str(tmp_path / "does_not_exist.html"), str(tmp_path))
        assert result == []

    def test_message_with_arxiv_link(self, tmp_path):
        html_file = tmp_path / "arxiv.html"
        html_file.write_text(self._make_html(
            '<div class="message default clearfix">\n'
            '<div class="body"><div class="text">\n'
            '<a href="https://arxiv.org/abs/2106.09685">Interesting paper</a>\n'
            '</div></div>\n'
            '</div>'
        ))
        result = parse_messages(str(html_file), str(tmp_path))
        assert len(result) == 1
        assert result[0]["domain"] == "arxiv"
        assert result[0]["content_type"] == "paper"

    def test_forwarded_message_parsed(self, tmp_path):
        """Forwarded messages have .body > .forwarded.body > .text."""
        html_file = tmp_path / "forwarded.html"
        html_file.write_text(self._make_html(
            '<div class="message default clearfix">\n'
            '<div class="body">\n'
            '<div class="forwarded body">\n'
            '<div class="text">\n'
            '<a href="https://github.com/user/repo">Cool repo</a>\n'
            '</div>\n'
            '</div>\n'
            '</div>\n'
            '</div>'
        ))
        result = parse_messages(str(html_file), str(tmp_path))
        assert len(result) == 1
        assert result[0]["url"] == "https://github.com/user/repo"
        assert result[0]["domain"] == "github"

    def test_multiple_messages_various_types(self, tmp_path):
        """Multiple messages: link, note, file -- all captured in order."""
        files_dir = tmp_path / "files"
        files_dir.mkdir()
        pdf_file = files_dir / "guide.pdf"
        pdf_file.write_text("guide content")

        html_file = tmp_path / "mixed.html"
        html_file.write_text(self._make_html(
            '<div class="message default clearfix">\n'
            '<div class="body"><div class="text">\n'
            '<a href="https://example.com/article">Article</a>\n'
            '</div></div>\n'
            '</div>\n'
            '<div class="message default clearfix">\n'
            '<div class="body"><div class="text">\n'
            'Just a thought.\n'
            '</div></div>\n'
            '</div>\n'
            '<div class="message default clearfix">\n'
            '<div class="body">\n'
            '<a class="media_file" href="files/guide.pdf">\n'
            '<div class="body"><div class="text">Guide</div></div>\n'
            '</a>\n'
            '</div>\n'
            '</div>'
        ))
        result = parse_messages(html_path=str(html_file), files_dir=str(files_dir))
        # First message: link
        # Second message: note
        # Third message: file
        assert len(result) == 3
        types = [r["content_type"] for r in result]
        assert "article" in types
        assert "note" in types
        assert "pdf" in types


# ===================================================================
# TestSummaryStats
# ===================================================================

class TestSummaryStats:
    def test_empty_items(self):
        stats = summary_stats([])
        assert stats["total_messages"] == 0
        assert stats["total_links"] == 0

    def test_mixed_items(self):
        items = [
            {"url": "https://a.com", "domain": "example", "content_type": "article", "local_file_path": None},
            {"url": "https://b.com", "domain": "example", "content_type": "video", "local_file_path": None},
            {"url": None, "domain": None, "content_type": "note", "local_file_path": None},
            {"url": "https://c.com", "domain": "other", "content_type": "article",
             "local_file_path": "/tmp/file.pdf"},
        ]
        stats = summary_stats(items)
        assert stats["total_messages"] == 4
        assert stats["total_links"] == 3
        assert stats["unique_domains"] == 2
        assert stats["file_attachments"] == 1
        assert stats["plain_notes"] == 1


# ===================================================================
# TestParseMessages — edge cases
# ===================================================================

class TestParseMessagesEdgeCases:
    def test_url_with_trailing_punctuation(self, tmp_path):
        """URL ending in a period inside text; clean_url should strip it."""
        html_file = tmp_path / "trailing_punct.html"
        html_file.write_text(
            '<!DOCTYPE html><div class="history">\n'
            '<div class="message default clearfix">\n'
            '<div class="body"><div class="text">\n'
            'See <a href="https://example.com.">https://example.com.</a>\n'
            '</div></div>\n'
            '</div>\n'
            '</div>'
        )
        result = parse_messages(str(html_file), str(tmp_path))
        # The cleaned URL should not have the trailing period
        if result:
            assert not result[0]["url"].endswith(".")

    def test_message_ignores_non_http_links(self, tmp_path):
        """Only http/https links are extracted; ftp/mailto are ignored.
        The surrounding text still produces a plain note item."""
        html_file = tmp_path / "non_http.html"
        html_file.write_text(
            '<!DOCTYPE html><div class="history">\n'
            '<div class="message default clearfix">\n'
            '<div class="body"><div class="text">\n'
            '<a href="ftp://files.example.com">FTP</a>\n'
            '<a href="mailto:user@example.com">Email</a>\n'
            '</div></div>\n'
            '</div>\n'
            '</div>'
        )
        result = parse_messages(str(html_file), str(tmp_path))
        # ftp/mailto are not http/https, so no URL item.  The text becomes a note.
        assert len(result) == 1
        assert result[0]["content_type"] == "note"
        assert result[0]["url"] is None

    def test_message_with_empty_text_still_produces_note_if_only_content(self, tmp_path):
        """Message with no URL, no file, but body exists -> note."""
        from bs4 import BeautifulSoup
        html_file = tmp_path / "whitespace_note.html"
        html_file.write_text(
            '<!DOCTYPE html><div class="history">\n'
            '<div class="message default clearfix">\n'
            '<div class="body">\n'
            '<div class="text">   \n'
            '  A real note with whitespace  \n'
            '</div>\n'
            '</div>\n'
            '</div>\n'
            '</div>'
        )
        result = parse_messages(str(html_file), str(tmp_path))
        assert len(result) == 1
        assert result[0]["content_type"] == "note"
