"""Tests for cover_fetcher.scraper"""

from __future__ import annotations

import base64

from cover_fetcher.scraper import (
    _extract_first_thumbnail,
    _extract_candidate_image_urls,
    _build_headers,
    build_query,
)
from fake_useragent import UserAgent


# ---------------------------------------------------------------------------
# build_query
# ---------------------------------------------------------------------------


def test_build_query_all_fields():
    q = build_query("The Beatles", "Abbey Road", "LP", "Apple", "1969", "discogs")
    assert "The Beatles" in q
    assert "Abbey Road" in q
    assert "LP" in q
    assert "Apple" in q
    assert "1969" in q
    assert "discogs" in q


def test_build_query_no_marketplace():
    q = build_query("Miles Davis", "Kind of Blue", "LP", "Columbia", "1959")
    assert "discogs" not in q
    assert "Miles Davis" in q


def test_build_query_empty_fields_skipped():
    q = build_query("Artist", "", "LP", "", "2000")
    assert "__" not in q  # no double spaces from empty fields


def test_build_query_sanitizes_quotes_slashes_and_dashes():
    q = build_query('2-Lux', '"25/Go !!!"', '12\"', 'A/B', "91-12", "discogs")
    assert '"' not in q
    assert "'" not in q
    assert "/" not in q
    assert "\\" not in q
    assert "-" not in q
    assert ";" not in q
    assert "2 Lux" in q
    assert "25 Go !!!" in q
    assert "91 12" in q
    assert "discogs" in q


def test_extract_candidate_image_urls_from_ou_payload():
    html = '<script>{"ou":"https:\\/\\/example.com\\/covers\\/record.jpg"}</script>'
    urls = _extract_candidate_image_urls(html)
    assert urls[0] == "https://example.com/covers/record.jpg"


# ---------------------------------------------------------------------------
# _extract_first_thumbnail
# ---------------------------------------------------------------------------


def _make_html_with_b64(data: bytes, mime: str = "jpeg") -> str:
    b64 = base64.b64encode(data).decode()
    return f'<script>var x = "data:image/{mime};base64,{b64}";</script>'


def test_extract_thumbnail_found():
    fake_image = b"\xff\xd8\xff" + b"\x00" * 600  # fake JPEG-like data, >512 bytes
    html = _make_html_with_b64(fake_image)
    result = _extract_first_thumbnail(html, min_image_bytes=512)
    assert result == fake_image


def test_extract_thumbnail_too_small():
    tiny = b"\xff\xd8\xff" + b"\x00" * 10  # too small
    html = _make_html_with_b64(tiny)
    result = _extract_first_thumbnail(html, min_image_bytes=512)
    assert result is None


def test_extract_thumbnail_no_data():
    result = _extract_first_thumbnail("<html>no images here</html>")
    assert result is None


def test_extract_thumbnail_png():
    fake_png = b"\x89PNG\r\n" + b"\x00" * 600
    html = _make_html_with_b64(fake_png, mime="png")
    result = _extract_first_thumbnail(html, min_image_bytes=512)
    assert result == fake_png


def test_extract_thumbnail_from_set_images_src_payload():
    fake_image = b"\xff\xd8\xff" + b"\x00" * 700
    b64 = base64.b64encode(fake_image).decode()
    html = f"<script>var s='data:image/jpeg;base64,{b64}'; var ii=[1]; _setImagesSrc(ii,s);</script>"
    result = _extract_first_thumbnail(html, min_image_bytes=512)
    assert result == fake_image


# ---------------------------------------------------------------------------
# _build_headers
# ---------------------------------------------------------------------------


def test_build_headers_has_user_agent():
    ua = UserAgent()
    headers = _build_headers(ua)
    assert "User-Agent" in headers
    assert len(headers["User-Agent"]) > 10
