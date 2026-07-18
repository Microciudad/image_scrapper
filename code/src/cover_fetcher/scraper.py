"""Google Images scraper that extracts base64-encoded thumbnails.

- Randomised User-Agent rotation via fake-useragent
- Browser-like request headers to avoid blocks
- Base64 thumbnail extraction from Google Images HTML
- Retry logic with back-off
- Browser fallback for Google challenge pages
"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import html as html_lib
import logging
import random
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional dependency at runtime
    PlaywrightError = Exception
    sync_playwright = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GOOGLE_IMAGES_URL = "https://www.google.com/search"
_MIN_REQUEST_DELAY_SECONDS = 0.5
_MAX_REQUEST_DELAY_SECONDS = 1.75
_BROWSER_WAIT_MS = 2500
_CAPTCHA_COOLDOWN_BASE_SECONDS = 5
_CAPTCHA_COOLDOWN_MAX_SECONDS = 25

# Google wraps inline thumbnails inside JS – these patterns cover the common
# encoding variants found in Images results pages.
_B64_PATTERN = re.compile(
    r'data:image/(?:jpeg|jpg|png|webp|gif);base64,([A-Za-z0-9+/=]+)',
    re.IGNORECASE,
)
_SET_IMAGE_SRC_PATTERN = re.compile(
    r"var\s+s='(data:image/(?:jpeg|jpg|png|webp|gif|jpe);base64,[^']*)';\s*"
    r"var\s+ii=\[[^\]]+\];\s*_setImagesSrc\(ii,s\);",
    re.IGNORECASE,
)
_OU_URL_PATTERN = re.compile(r'"ou":"(https?:(?:\\/|[^"\\])+)"')
_IMG_URL_PATTERN = re.compile(r'"(https?://[^"\\]+(?:jpg|jpeg|png|webp)(?:\?[^"\\]*)?)"', re.IGNORECASE)
_DIRECT_IMG_URL_PATTERN = re.compile(r'https?://[^\s<>"]+\.(?:jpg|jpeg|jpe|png|webp|gif)[^\s<>"]*', re.IGNORECASE)
_GSTATIC_URL_PATTERN = re.compile(r'https://encrypted-tbn0\.gstatic\.com/images\?[^\s"\'<>]*', re.IGNORECASE)
_DISCOGS_IMAGE_URL_PATTERN = re.compile(r'https?:\\?/\\?/(?:i|img)\.discogs\.com/[^\s"\'<>]+', re.IGNORECASE)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

_QUERY_LANGUAGE_POOL = ["en", "es", "fr", "de", "it", "pt", "en-US", "en-GB"]
_REFERRER_POOL = [
    "https://www.google.com/",
    "https://www.google.es/",
    "https://www.duckduckgo.com/",
    "https://www.startpage.com/",
]

_ACCEPT_LANGUAGE_POOL = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.8,en-US;q=0.7",
    "es-ES,es;q=0.9,en;q=0.8",
    "fr-FR,fr;q=0.9,en;q=0.8",
    "de-DE,de;q=0.9,en;q=0.8",
]

_MIN_IMAGE_BYTES = 512   # ignore tiny / blank placeholders
_MAX_IMAGE_BYTES = 50_000  # thumbnails are always small; cap avoids anomalies
_STRIP_QUERY_CHARS = re.compile(r"[\"'\\/\\\-;]+")

_THREAD_STATE = threading.local()
_STATE_LOCK = threading.Lock()
_BROWSER_FALLBACK_LOCK = threading.Lock()
_CAPTCHA_EVENT_COUNT = 0
_CAPTCHA_COOLDOWN_UNTIL = 0.0

_PLAYWRIGHT_INSTANCE: Any = None
_PLAYWRIGHT_BROWSER: Any = None
_PLAYWRIGHT_CONTEXT: Any = None
_PLAYWRIGHT_PAGE: Any = None
_PLAYWRIGHT_HEADLESS: Optional[bool] = None
_PLAYWRIGHT_OWNER_THREAD_ID: Optional[int] = None
_BROWSER_FALLBACK_EXECUTOR: Optional[ThreadPoolExecutor] = None

_RETRY_STRATEGY = Retry(
    total=1,
    backoff_factor=0.5,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["GET", "HEAD"],
)
_ADAPTER = HTTPAdapter(max_retries=_RETRY_STRATEGY)

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def build_query(
    artist: str,
    title: str,
    format_: str,
    label: str,
    year: str,
    marketplace: str = "",
    edition_country: str = "",
) -> str:
    """Return the Google search query string for a record cover."""
    parts = [_sanitize_query_part(p) for p in [artist, title, format_, label, year, edition_country]]
    parts = [p for p in parts if p]

    market = _sanitize_query_part(marketplace)
    if market:
        parts.append(market)

    return " ".join(parts)


def fetch_cover(
    query: str,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    min_image_bytes: int = _MIN_IMAGE_BYTES,
    dump_html_path: Optional[Path] = None,
    discogs_hq: bool = False,
    final_html_path: Optional[Path] = None,
    final_html_format: str = "mhtml",
    browser_fallback: bool = True,
    browser_headless: bool = True,
    captcha_wait_seconds: int = 300,
    reuse_browser_session: bool = True,
    captcha_cooldown_base_seconds: int = _CAPTCHA_COOLDOWN_BASE_SECONDS,
    captcha_cooldown_max_seconds: int = _CAPTCHA_COOLDOWN_MAX_SECONDS,
    request_delay_min_seconds: float = _MIN_REQUEST_DELAY_SECONDS,
    request_delay_max_seconds: float = _MAX_REQUEST_DELAY_SECONDS,
    request_label: str = "",
    image_index: int = 1,
) -> Optional[bytes]:
    """Search Google Images and return the first valid thumbnail as raw bytes.

    The function extracts the *inline* base64-encoded thumbnail that Google
    embeds directly in the search-results HTML, so no secondary requests to
    third-party image hosts are made.

    Args:
        query: The search query string.
        max_retries: How many times to retry on network or blocking failures.
        retry_delay: Base delay (seconds) between retries (jitter is added).
        min_image_bytes: Minimum decoded image size to consider valid.
        dump_html_path: Optional path where raw Google HTML responses are written.
        discogs_hq: If True, prioritize high-resolution Discogs CDN URLs.
        final_html_path: Optional path where the final successful Google results
            page is saved for this fetched image.
        final_html_format: Output format for final_html_path, "mhtml" or "html".
        browser_fallback: If True, retry in a real browser when Google challenges
            the raw HTTP client.
        browser_headless: Run the browser fallback headlessly when enabled.
        captcha_wait_seconds: Seconds to wait for manual CAPTCHA solving in browser.
        reuse_browser_session: Reuse one browser context across rows to keep
            cookies/session and reduce challenge frequency.
        captcha_cooldown_base_seconds: Initial cooldown after each block/CAPTCHA.
        captcha_cooldown_max_seconds: Maximum adaptive cooldown after repeated blocks.
        request_label: Optional human-readable worker/job label for logging.
        image_index: Which image from search results to use (1-indexed). Default 1 for first image.

    Returns:
        Raw image bytes, or ``None`` if no suitable image was found.
    """
    logger.info("[DEBUG] fetch_cover START: query=%r image_index=%d discogs_hq=%s browser_fallback=%s", query, image_index, discogs_hq, browser_fallback)
    
    # Store request delay range in thread-local state for use in _apply_delay()
    _THREAD_STATE.request_delay_min = request_delay_min_seconds
    _THREAD_STATE.request_delay_max = request_delay_max_seconds
    
    session = _get_thread_session()
    search_queries = _build_search_queries(query)
    logger.debug("Google query variants: %s", search_queries)

    request_number = 0
    for search_query in search_queries:
        for attempt in range(1, max_retries + 1):
            request_number += 1
            try:
                _apply_adaptive_cooldown()
                blocked_response = False
                _apply_delay()
                headers = _build_headers(referrer="https://www.google.com/")
                params = _build_search_params(search_query)
                prepared_url = requests.Request("GET", _GOOGLE_IMAGES_URL, params=params).prepare().url
                if request_label:
                    logger.info("Google request URL for %s: %s", request_label, prepared_url)
                else:
                    logger.info("Google request URL: %s", prepared_url)
                logger.debug("GET %s  q=%r  (variant %r attempt %d)", _GOOGLE_IMAGES_URL, search_query, search_query, attempt)
                response = session.get(
                    _GOOGLE_IMAGES_URL,
                    params=params,
                    headers=headers,
                    timeout=30,
                    allow_redirects=True,
                )
                logger.debug("Google response status=%s final_url=%s", response.status_code, response.url)

                if "/sorry" in response.url:
                    logger.warning("Google redirected to /sorry blocking page")
                    blocked_response = True
                    _register_captcha_or_block_event(
                        source="http_sorry",
                        base_seconds=captcha_cooldown_base_seconds,
                        max_seconds=captcha_cooldown_max_seconds,
                    )
                    _rotate_session()

                response.raise_for_status()

                if dump_html_path is not None:
                    _dump_google_html(response.text, search_query, request_number, dump_html_path)

                if "enablejs" in response.text or "SG_REL" in response.text:
                    logger.warning("Google returned anti-bot/enablejs interstitial instead of image results")
                    blocked_response = True
                    _register_captcha_or_block_event(
                        source="http_interstitial",
                        base_seconds=captcha_cooldown_base_seconds,
                        max_seconds=captcha_cooldown_max_seconds,
                    )
                    _rotate_session()

                if blocked_response:
                    if browser_fallback:
                        if request_label:
                            logger.info(
                                "Google challenged the HTTP client for %s; falling back to a real browser session",
                                request_label,
                            )
                        else:
                            logger.info("Google challenged the HTTP client; falling back to a real browser session")
                        image_bytes = _run_browser_fallback(
                            search_query,
                            min_image_bytes=min_image_bytes,
                            dump_html_path=dump_html_path,
                            discogs_hq=discogs_hq,
                            final_html_path=final_html_path,
                            final_html_format=final_html_format,
                            attempt=request_number,
                            headless=browser_headless,
                            captcha_wait_seconds=captcha_wait_seconds,
                            reuse_browser_session=reuse_browser_session,
                            captcha_cooldown_base_seconds=captcha_cooldown_base_seconds,
                            captcha_cooldown_max_seconds=captcha_cooldown_max_seconds,
                            request_label=request_label,
                            image_index=image_index,
                        )
                        if image_bytes:
                            return image_bytes
                        logger.warning("Browser fallback did not yield a usable image")
                    logger.warning("Stopping after blocked Google response; skipping further retries and query variants")
                    return None

                if discogs_hq and image_index == 1:
                    # Only use Discogs URL extraction for the first image
                    # For indexed images, fall through to screenshot path which respects image grid position
                    discogs_candidates = _extract_discogs_image_urls(response.text)
                    if discogs_candidates:
                        logger.info("Discogs HQ candidates found: %d", len(discogs_candidates))
                    for image_url in discogs_candidates[:24]:
                        logger.info("Discogs HQ URL: %s", image_url)
                        image_bytes = _download_image_url(image_url, headers, min_image_bytes, session=session)
                        if image_bytes:
                            _save_final_google_snapshot(
                                final_html_path,
                                page=None,
                                html_fallback=response.text,
                                output_format=final_html_format,
                            )
                            logger.info("Downloaded Discogs HQ image (%d bytes)", len(image_bytes))
                            return image_bytes
                elif discogs_hq and image_index > 1:
                    # Image index > 1: need browser/screenshot to respect grid position
                    # Force browser fallback
                    logger.info(
                        "Image index %d with Discogs HQ: forcing browser fallback to use screenshot method for indexed image",
                        image_index,
                    )
                    if browser_fallback:
                        image_bytes = _run_browser_fallback(
                            search_query,
                            min_image_bytes=min_image_bytes,
                            dump_html_path=dump_html_path,
                            discogs_hq=discogs_hq,
                            final_html_path=final_html_path,
                            final_html_format=final_html_format,
                            attempt=request_number,
                            headless=browser_headless,
                            captcha_wait_seconds=captcha_wait_seconds,
                            reuse_browser_session=reuse_browser_session,
                            captcha_cooldown_base_seconds=captcha_cooldown_base_seconds,
                            captcha_cooldown_max_seconds=captcha_cooldown_max_seconds,
                            request_label=request_label,
                            image_index=image_index,
                        )
                        if image_bytes:
                            return image_bytes
                    logger.warning("Browser fallback disabled; cannot use indexed image selection without browser")
                    break

                image_bytes = _extract_first_thumbnail(response.text, min_image_bytes)
                if image_bytes and image_index == 1:
                    _save_final_google_snapshot(
                        final_html_path,
                        page=None,
                        html_fallback=response.text,
                        output_format=final_html_format,
                    )
                    logger.debug("Found thumbnail (%d bytes) on request %d", len(image_bytes), request_number)
                    return image_bytes

                candidates = _extract_candidate_image_urls(response.text)
                if candidates:
                    logger.info("Fallback candidate URLs found: %d", len(candidates))
                for image_url in candidates[:12]:
                    if image_index > 1:
                        logger.debug("Skipping candidate URL extraction for image_index %d (requires browser screenshot)", image_index)
                        break
                    logger.info("Fallback image URL: %s", image_url)
                    image_bytes = _download_image_url(image_url, headers, min_image_bytes, session=session)
                    if image_bytes:
                        _save_final_google_snapshot(
                            final_html_path,
                            page=None,
                            html_fallback=response.text,
                            output_format=final_html_format,
                        )
                        logger.debug("Downloaded fallback image (%d bytes) on request %d", len(image_bytes), request_number)
                        return image_bytes

                logger.warning("No suitable thumbnail on attempt %d for query %r", attempt, search_query)
                break

            except requests.RequestException as exc:
                logger.warning("Request error on attempt %d for query %r: %s", attempt, search_query, exc)

            if attempt < max_retries:
                delay = retry_delay * attempt + random.uniform(0.25, 0.75)
                logger.debug("Waiting %.1f s before retry", delay)
                time.sleep(delay)

    logger.error("All %d attempts failed for query %r", max_retries, query)
    return None


def _run_browser_fallback(
    query: str,
    min_image_bytes: int,
    dump_html_path: Optional[Path],
    discogs_hq: bool,
    final_html_path: Optional[Path],
    final_html_format: str,
    attempt: int,
    headless: bool,
    captcha_wait_seconds: int,
    reuse_browser_session: bool,
    captcha_cooldown_base_seconds: int,
    captcha_cooldown_max_seconds: int,
    request_label: str,
    image_index: int,
) -> Optional[bytes]:
    """Run browser fallback either directly or on the dedicated browser thread."""
    if reuse_browser_session:
        executor = _get_browser_fallback_executor()
        future = executor.submit(
            _fetch_cover_via_browser,
            query,
            min_image_bytes,
            dump_html_path,
            discogs_hq,
            final_html_path,
            final_html_format,
            attempt,
            headless,
            captcha_wait_seconds,
            reuse_browser_session,
            captcha_cooldown_base_seconds,
            captcha_cooldown_max_seconds,
            request_label,
            image_index,
        )
        return future.result()

    with _BROWSER_FALLBACK_LOCK:
        return _fetch_cover_via_browser(
            query,
            min_image_bytes,
            dump_html_path,
            discogs_hq,
            final_html_path,
            final_html_format,
            attempt,
            headless,
            captcha_wait_seconds,
            reuse_browser_session,
            captcha_cooldown_base_seconds,
            captcha_cooldown_max_seconds,
            request_label,
            image_index,
        )


def _get_browser_fallback_executor() -> ThreadPoolExecutor:
    """Return dedicated single-thread executor for reusable browser fallback."""
    global _BROWSER_FALLBACK_EXECUTOR
    with _BROWSER_FALLBACK_LOCK:
        if _BROWSER_FALLBACK_EXECUTOR is None:
            _BROWSER_FALLBACK_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser-fallback")
        return _BROWSER_FALLBACK_EXECUTOR


def close_browser_session() -> None:
    """Close persistent Playwright resources if they were created."""
    global _BROWSER_FALLBACK_EXECUTOR

    if _BROWSER_FALLBACK_EXECUTOR is not None:
        executor = _BROWSER_FALLBACK_EXECUTOR
        _BROWSER_FALLBACK_EXECUTOR = None
        try:
            future = executor.submit(_close_browser_session_resources)
            future.result(timeout=10)
        except FutureTimeoutError:
            logger.warning("Timed out closing browser fallback executor resources")
        except Exception as exc:
            logger.debug("Browser fallback executor close raised: %s", exc)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return

    _close_browser_session_resources()


def _close_browser_session_resources() -> None:
    """Close persistent Playwright resources if they were created."""
    global _PLAYWRIGHT_CONTEXT, _PLAYWRIGHT_BROWSER, _PLAYWRIGHT_INSTANCE, _PLAYWRIGHT_PAGE, _PLAYWRIGHT_HEADLESS
    global _PLAYWRIGHT_OWNER_THREAD_ID

    with _BROWSER_FALLBACK_LOCK:
        try:
            if _PLAYWRIGHT_PAGE is not None:
                _PLAYWRIGHT_PAGE.close()
        except Exception:
            pass
        finally:
            _PLAYWRIGHT_PAGE = None

        try:
            if _PLAYWRIGHT_CONTEXT is not None:
                _PLAYWRIGHT_CONTEXT.close()
        except Exception:
            pass
        finally:
            _PLAYWRIGHT_CONTEXT = None

        try:
            if _PLAYWRIGHT_BROWSER is not None:
                _PLAYWRIGHT_BROWSER.close()
        except Exception:
            pass
        finally:
            _PLAYWRIGHT_BROWSER = None

        try:
            if _PLAYWRIGHT_INSTANCE is not None:
                _PLAYWRIGHT_INSTANCE.stop()
        except Exception:
            pass
        finally:
            _PLAYWRIGHT_INSTANCE = None
            _PLAYWRIGHT_HEADLESS = None
            _PLAYWRIGHT_OWNER_THREAD_ID = None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_headers(referrer: Optional[str] = None) -> dict[str, str]:
    """Return a realistic browser-like header dict."""
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(_ACCEPT_LANGUAGE_POOL),
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "Pragma": "no-cache",
        "Referer": referrer or random.choice(_REFERRER_POOL),
    }


def _extract_first_thumbnail(
    html: str,
    min_image_bytes: int = _MIN_IMAGE_BYTES,
) -> Optional[bytes]:
    """Scan *html* for the first usable base64-encoded image thumbnail.

    Google typically stores these as ``data:image/jpeg;base64,...`` literals
    inside ``<script>`` blocks.  We iterate over all matches and return the
    first that decodes to a plausible image size.

    Args:
        html: The raw HTML text of the Google Images results page.
        min_image_bytes: Minimum decoded byte count to accept.

    Returns:
        Decoded image bytes, or ``None``.
    """
    b64_candidates: list[str] = []

    # Preferred Google-specific pattern used in working sample.
    for match in _SET_IMAGE_SRC_PATTERN.finditer(html):
        data_url = match.group(1)
        if "," in data_url:
            b64_candidates.append(data_url.split(",", 1)[1])

    # Generic fallback pattern.
    for match in _B64_PATTERN.finditer(html):
        b64_candidates.append(match.group(1))

    for b64_data in b64_candidates:
        b64_data = _normalize_b64_string(b64_data)
        # Pad if necessary (Google sometimes strips trailing '=')
        padding = (4 - len(b64_data) % 4) % 4
        b64_data += "=" * padding
        try:
            raw = base64.b64decode(b64_data)
        except Exception:
            continue
        if min_image_bytes <= len(raw) <= _MAX_IMAGE_BYTES:
            return raw

    return None


def _extract_candidate_image_urls(html: str) -> list[str]:
    """Extract likely image URLs from multiple Google response shapes."""
    urls: list[str] = []

    for pattern in (_OU_URL_PATTERN, _IMG_URL_PATTERN):
        for match in pattern.finditer(html):
            url = match.group(1).replace("\\/", "/")
            urls.append(url)

    urls.extend(_DIRECT_IMG_URL_PATTERN.findall(html))
    urls.extend(_GSTATIC_URL_PATTERN.findall(html))

    # De-duplicate while preserving order and skip clearly useless links.
    seen: set[str] = set()
    clean: list[str] = []
    for url in urls:
        if not url or url in seen:
            continue
        if any(x in url.lower() for x in ("logo", "icon", "spacer", "pixel", "google.com/images/branding")):
            continue
        seen.add(url)
        clean.append(url)
    return clean


def _extract_discogs_image_urls(html: str) -> list[str]:
    """Extract and prioritize Discogs CDN image URLs from Google payloads."""
    urls: list[str] = []

    for match in _DISCOGS_IMAGE_URL_PATTERN.finditer(html):
        raw = match.group(0)
        normalized = raw.replace("\\/", "/")
        normalized = html_lib.unescape(normalized)
        urls.append(normalized)

    for candidate in _extract_candidate_image_urls(html):
        if _is_discogs_image_url(candidate):
            urls.append(candidate)

    # Prefer i.discogs.com over generic img.discogs.com and keep order stable.
    seen: set[str] = set()
    preferred: list[str] = []
    secondary: list[str] = []
    for url in urls:
        cleaned = url.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        if "i.discogs.com/" in cleaned.lower():
            preferred.append(cleaned)
        else:
            secondary.append(cleaned)

    return preferred + secondary


def _prioritize_candidate_by_index(candidates: list[str], image_index: int) -> list[str]:
    """Return candidates ordered so requested 1-indexed position is tried first."""
    if not candidates:
        return []

    safe_index = max(1, image_index)
    target = safe_index - 1
    if target >= len(candidates):
        logger.warning(
            "Requested image index %d exceeds available candidates (%d); using first candidate",
            safe_index,
            len(candidates),
        )
        return candidates

    return [candidates[target], *candidates[:target], *candidates[target + 1 :]]


def _download_image_url(
    image_url: str,
    base_headers: dict[str, str],
    min_image_bytes: int,
    session: Optional[requests.Session] = None,
) -> Optional[bytes]:
    """Download fallback direct image URL and return bytes if plausible."""
    image_headers = dict(base_headers)
    image_headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
    image_headers["Referer"] = "https://www.discogs.com/" if _is_discogs_image_url(image_url) else "https://www.google.com/"
    session = session or _get_thread_session()
    try:
        response = session.get(image_url, headers=image_headers, timeout=15, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.debug("Fallback image request failed: %s", exc)
        return None

    content_type = response.headers.get("Content-Type", "").lower()
    if "image" not in content_type and content_type:
        return None

    raw = response.content
    if min_image_bytes <= len(raw) <= 6_000_000:
        return raw
    return None


def _is_discogs_image_url(url: str) -> bool:
    lowered = (url or "").lower()
    return "i.discogs.com/" in lowered or "img.discogs.com/" in lowered


def _normalize_b64_string(data: str) -> str:
    """Normalize escaped base64 fragments seen in Google script payloads."""
    cleaned = data.replace("\\x3d", "=").replace("\\u003d", "=")
    cleaned = cleaned.replace("\\n", "").replace("\\/", "/")
    return cleaned


def _dump_google_html(html: str, query: str, attempt: int, dump_path: Path) -> None:
    """Write raw Google HTML response to disk in plain UTF-8 text."""
    try:
        if dump_path.suffix.lower() == ".html":
            target = dump_path
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            dump_path.mkdir(parents=True, exist_ok=True)
            slug = _slugify(query)[:80] or "query"
            target = dump_path / f"google_{slug}_attempt{attempt}.html"

        target.write_text(html, encoding="utf-8", errors="ignore")
        logger.info("Dumped Google HTML: %s", target)
    except OSError as exc:
        logger.warning("Could not dump Google HTML to %s: %s", dump_path, exc)


def _save_final_google_snapshot(
    path: Optional[Path],
    page: Any,
    html_fallback: str,
    output_format: str = "mhtml",
) -> None:
    """Save final Google page as MHTML or plain HTML."""
    if path is None:
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        if output_format == "html":
            path.write_text(html_fallback, encoding="utf-8", errors="ignore")
            logger.info("Saved final Google HTML: %s", path)
            return

        mhtml: Optional[str] = None
        if page is not None:
            try:
                cdp = page.context.new_cdp_session(page)
                mhtml = cdp.send("Page.captureSnapshot", {"format": "mhtml"}).get("data")
            except Exception as exc:
                logger.warning("Could not capture MHTML snapshot via browser session: %s", exc)

        if mhtml:
            path.write_text(mhtml, encoding="utf-8", errors="ignore")
            logger.info("Saved final Google MHTML: %s", path)
            return

        # Fallback for non-browser path or CDP capture failures.
        path.write_text(html_fallback, encoding="utf-8", errors="ignore")
        logger.info("Saved fallback page source (HTML text) at: %s", path)
    except OSError as exc:
        logger.warning("Could not save final Google page snapshot to %s: %s", path, exc)


def _slugify(value: str) -> str:
    """Create a safe filename fragment from arbitrary text."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_")
    return slug.lower()


def _apply_delay() -> None:
    """Apply small random delay to avoid highly bot-like request pacing."""
    last_request_time = getattr(_THREAD_STATE, "last_request_time", 0.0)
    elapsed = time.time() - last_request_time
    # Use thread-local delay range if set, otherwise use module constants
    delay_min = getattr(_THREAD_STATE, "request_delay_min", _MIN_REQUEST_DELAY_SECONDS)
    delay_max = getattr(_THREAD_STATE, "request_delay_max", _MAX_REQUEST_DELAY_SECONDS)
    delay = random.uniform(delay_min, delay_max)
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _THREAD_STATE.last_request_time = time.time()


def _build_search_params(query: str) -> dict[str, str | int]:
    """Build Google Images params using the same randomization strategy as the sample scraper."""
    params: dict[str, str | int] = {
        "q": query,
        "tbm": "isch",
        "hl": random.choice(_QUERY_LANGUAGE_POOL),
    }

    return params


def _build_search_queries(query: str) -> list[str]:
    """Build shuffled query variants similar to the sample scraper strategy."""
    clean_query = query.replace('"', '').strip()
    queries = [clean_query]

    deduped: list[str] = []
    seen: set[str] = set()
    for item in queries:
        normalized = " ".join(item.split())
        if normalized and normalized not in seen:
            deduped.append(normalized)
            seen.add(normalized)

    random.shuffle(deduped)
    return deduped


def _fetch_cover_via_browser(
    query: str,
    min_image_bytes: int,
    dump_html_path: Optional[Path],
    discogs_hq: bool,
    final_html_path: Optional[Path],
    final_html_format: str,
    attempt: int,
    headless: bool,
    captcha_wait_seconds: int = 300,
    reuse_browser_session: bool = True,
    captcha_cooldown_base_seconds: int = _CAPTCHA_COOLDOWN_BASE_SECONDS,
    captcha_cooldown_max_seconds: int = _CAPTCHA_COOLDOWN_MAX_SECONDS,
    request_label: str = "",
    image_index: int = 1,
) -> Optional[bytes]:
    """Fetch thumbnails via a real browser when Google challenges requests."""
    logger.info("[DEBUG] _fetch_cover_via_browser: image_index=%d discogs_hq=%s", image_index, discogs_hq)
    
    if sync_playwright is None:
        logger.warning("Browser fallback requested but Playwright is not installed")
        return None

    params = _build_search_params(query)
    prepared_url = requests.Request("GET", _GOOGLE_IMAGES_URL, params=params).prepare().url
    if request_label:
        logger.info("Browser fallback URL for %s: %s", request_label, prepared_url)
    else:
        logger.info("Browser fallback URL: %s", prepared_url)

    page = None
    close_page_on_exit = True
    allow_persistent_reuse = reuse_browser_session and _can_reuse_persistent_browser_on_current_thread()
    try:
        if allow_persistent_reuse:
            page = _get_or_create_persistent_page(headless=headless)
            close_page_on_exit = False
            page.goto(prepared_url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(_BROWSER_WAIT_MS)
            return _extract_image_from_browser_page(
                page=page,
                query=query,
                min_image_bytes=min_image_bytes,
                dump_html_path=dump_html_path,
                discogs_hq=discogs_hq,
                final_html_path=final_html_path,
                final_html_format=final_html_format,
                attempt=attempt,
                captcha_wait_seconds=captcha_wait_seconds,
                captcha_cooldown_base_seconds=captcha_cooldown_base_seconds,
                captcha_cooldown_max_seconds=captcha_cooldown_max_seconds,
                request_label=request_label,
                image_index=image_index,
            )

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright, headless)
            context = browser.new_context(
                locale="en-US",
                user_agent=random.choice(_USER_AGENTS),
                viewport={"width": 1440, "height": 1080},
            )
            page = context.new_page()
            page.goto(prepared_url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(_BROWSER_WAIT_MS)
            return _extract_image_from_browser_page(
                page=page,
                query=query,
                min_image_bytes=min_image_bytes,
                dump_html_path=dump_html_path,
                discogs_hq=discogs_hq,
                final_html_path=final_html_path,
                final_html_format=final_html_format,
                attempt=attempt,
                captcha_wait_seconds=captcha_wait_seconds,
                captcha_cooldown_base_seconds=captcha_cooldown_base_seconds,
                captcha_cooldown_max_seconds=captcha_cooldown_max_seconds,
                request_label=request_label,
                    image_index=image_index,
            )
    except PlaywrightError as exc:
        logger.warning("Browser fallback encountered an error: %s", exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error in browser fallback: %s", exc)
        return None
    finally:
        try:
            if close_page_on_exit and page is not None:
                page.close()
        except Exception:
            pass


def _extract_image_from_browser_page(
                page: Any,
                query: str,
                min_image_bytes: int,
                dump_html_path: Optional[Path],
                discogs_hq: bool,
                final_html_path: Optional[Path],
                final_html_format: str,
                attempt: int,
                captcha_wait_seconds: int,
                captcha_cooldown_base_seconds: int,
                captcha_cooldown_max_seconds: int,
                request_label: str,
                image_index: int,
            ) -> Optional[bytes]:
                """Extract image bytes from a loaded browser page, with CAPTCHA handling."""
                if _has_captcha(page.content(), page):
                    _register_captcha_or_block_event(
                        source="browser_captcha",
                        base_seconds=captcha_cooldown_base_seconds,
                        max_seconds=captcha_cooldown_max_seconds,
                    )
                    label_text = f" for {request_label}" if request_label else ""
                    logger.warning(
                        "Google CAPTCHA detected%s. Keeping browser window open for manual intervention.",
                        label_text,
                    )
                    console = None
                    try:
                        from rich.console import Console

                        console = Console()
                        console.print("[bold yellow]⚠️  CAPTCHA DETECTED[/]")
                        if request_label:
                            console.print(f"[yellow]Worker waiting:[/] {request_label}")
                        console.print("[yellow]1. Solve the CAPTCHA in the browser window[/]")
                        console.print("[yellow]2. Once you see images appear, press ENTER in this terminal[/]")
                        console.print(f"[yellow]   OR wait for automatic detection (up to {captcha_wait_seconds}s)[/]")
                        console.print("[dim](The script will monitor and continue when ready)[/]")
                    except Exception:
                        print("⚠️  CAPTCHA DETECTED.")
                        if request_label:
                            print(f"Worker waiting: {request_label}")
                        print("1. Solve the CAPTCHA in the browser window")
                        print(f"2. Press ENTER here to continue, or wait up to {captcha_wait_seconds}s for auto-detection")

                    captcha_cleared = False
                    user_pressed_enter = False

                    def wait_for_user_signal() -> None:
                        nonlocal user_pressed_enter
                        try:
                            input()
                            user_pressed_enter = True
                            if request_label:
                                logger.info("User signaled CAPTCHA solved via Enter key for %s", request_label)
                            else:
                                logger.info("User signaled CAPTCHA solved via Enter key")
                        except Exception:
                            pass

                    signal_thread = threading.Thread(target=wait_for_user_signal, daemon=True)
                    signal_thread.start()

                    remaining_wait = captcha_wait_seconds
                    poll_interval = 10
                    while remaining_wait > 0 and not captcha_cleared:
                        if user_pressed_enter:
                            if request_label:
                                logger.info("User manually signaled - proceeding with page as-is for %s", request_label)
                            else:
                                logger.info("User manually signaled - proceeding with page as-is")
                            captcha_cleared = True
                            break

                        captcha_cleared = _wait_for_captcha_clear(page, min(poll_interval, remaining_wait))
                        remaining_wait -= poll_interval

                    if not captcha_cleared and not user_pressed_enter:
                        if request_label:
                            logger.warning("CAPTCHA solve timeout or user did not complete it for %s.", request_label)
                        else:
                            logger.warning("CAPTCHA solve timeout or user did not complete it.")
                        if console:
                            console.print("[red]Timeout reached. Proceeding with current page state...[/]")
                        page.wait_for_timeout(2_000)

                    if request_label:
                        logger.info("CAPTCHA appears to be solved for %s. Waiting for page to fully load...", request_label)
                    else:
                        logger.info("CAPTCHA appears to be solved. Waiting for page to fully load...")
                    _wait_for_images_to_render(page, timeout_ms=10_000)
                    page.wait_for_timeout(2_000)

                    if console:
                        console.print("[green]✓ CAPTCHA cleared, images loaded.[/]")

                html = page.content()
                if dump_html_path is not None:
                    _dump_google_html(html, f"{query} browser", attempt, dump_html_path)

                logger.debug("Extracted %d bytes of HTML after CAPTCHA/load", len(html))

                # When image_index > 1, prioritize screenshot to respect grid position
                if image_index > 1:
                    logger.info("Image index %d: using screenshot method to capture indexed image from grid", image_index)
                    image_bytes = _screenshot_first_result_image(page, min_image_bytes, image_index)
                    if image_bytes:
                        _save_final_google_snapshot(
                            final_html_path,
                            page=page,
                            html_fallback=html,
                            output_format=final_html_format,
                        )
                        logger.info("Captured screenshot of indexed image (%d bytes)", len(image_bytes))
                        return image_bytes
                    logger.warning("Could not capture indexed image via screenshot, falling back to other methods")

                if discogs_hq and image_index == 1:
                    # Only use Discogs URL extraction for the first image
                    headers = _build_headers(referrer="https://www.google.com/")
                    discogs_candidates = _extract_discogs_image_urls(html)
                    for image_url in _extract_browser_image_sources(page):
                        if _is_discogs_image_url(image_url):
                            discogs_candidates.append(image_url)

                    if discogs_candidates:
                        logger.info("Discogs HQ candidates found in browser page: %d", len(discogs_candidates))

                    # Deduplicate while preserving priority order.
                    seen_discogs: set[str] = set()
                    deduped_discogs: list[str] = []
                    for image_url in discogs_candidates:
                        if image_url and image_url not in seen_discogs:
                            seen_discogs.add(image_url)
                            deduped_discogs.append(image_url)

                    for image_url in deduped_discogs[:30]:
                        logger.debug("Trying Discogs HQ browser URL: %s", image_url)
                        image_bytes = _download_image_url(image_url, headers, min_image_bytes)
                        if image_bytes:
                            _save_final_google_snapshot(
                                final_html_path,
                                page=page,
                                html_fallback=html,
                                output_format=final_html_format,
                            )
                            logger.info("Downloaded Discogs HQ image from browser URL (%d bytes)", len(image_bytes))
                            return image_bytes
                elif discogs_hq and image_index > 1:
                    # Image index > 1: skip Discogs extraction and fall through to screenshot path
                    # which correctly respects grid image position
                    logger.info(
                        "Image index %d requested with Discogs HQ in browser mode: "
                        "skipping Discogs extraction to use screenshot method for indexed image",
                        image_index,
                    )

                image_bytes = _extract_first_thumbnail(html, min_image_bytes)
                if image_bytes and image_index == 1:
                    _save_final_google_snapshot(
                        final_html_path,
                        page=page,
                        html_fallback=html,
                        output_format=final_html_format,
                    )
                    logger.info("Found base64 thumbnail in HTML (%d bytes)", len(image_bytes))
                    return image_bytes

                headers = _build_headers(referrer="https://www.google.com/")
                candidates = _extract_candidate_image_urls(html)
                if candidates:
                    logger.info("Found %d candidate image URLs in HTML", len(candidates))
                for image_url in candidates[:12]:
                    if image_index > 1:
                        logger.debug("Skipping candidate URL extraction for image_index %d (use screenshot only)", image_index)
                        break
                    logger.debug("Trying candidate URL: %s", image_url)
                    image_bytes = _download_image_url(image_url, headers, min_image_bytes)
                    if image_bytes:
                        _save_final_google_snapshot(
                            final_html_path,
                            page=page,
                            html_fallback=html,
                            output_format=final_html_format,
                        )
                        logger.info("Downloaded candidate image (%d bytes) from URL", len(image_bytes))
                        return image_bytes

                logger.debug("Trying to extract image sources from rendered DOM...")
                for image_url in _extract_browser_image_sources(page):
                    if image_index > 1:
                        logger.debug("Skipping DOM image extraction for image_index %d (use screenshot only)", image_index)
                        break
                    logger.debug("Trying DOM image source: %s", image_url)
                    if image_url.startswith("data:image/"):
                        image_bytes = _decode_data_image_url(image_url, min_image_bytes)
                    else:
                        image_bytes = _download_image_url(image_url, headers, min_image_bytes)

                    if image_bytes:
                        _save_final_google_snapshot(
                            final_html_path,
                            page=page,
                            html_fallback=html,
                            output_format=final_html_format,
                        )
                        logger.info("Downloaded DOM image source (%d bytes)", len(image_bytes))
                        return image_bytes

                logger.debug("Attempting screenshot fallback...")
                if image_index > 1:
                    logger.info("Final attempt: screenshot method for image index %d", image_index)
                image_bytes = _screenshot_first_result_image(page, min_image_bytes, image_index)
                if image_bytes:
                    _save_final_google_snapshot(
                        final_html_path,
                        page=page,
                        html_fallback=html,
                        output_format=final_html_format,
                    )
                    logger.info("Captured screenshot of image (%d bytes)", len(image_bytes))
                    return image_bytes

                if image_index > 1:
                    logger.error("FAILED to capture image index %d via screenshot - no valid images found at that position", image_index)
                logger.warning("No images could be extracted from rendered page after CAPTCHA solve")
                return None


def _get_or_create_persistent_context(headless: bool):
                """Create or return a persistent browser context reused across rows."""
                global _PLAYWRIGHT_INSTANCE, _PLAYWRIGHT_BROWSER, _PLAYWRIGHT_CONTEXT, _PLAYWRIGHT_HEADLESS
                global _PLAYWRIGHT_OWNER_THREAD_ID

                if _PLAYWRIGHT_CONTEXT is not None and _PLAYWRIGHT_HEADLESS == headless:
                    return _PLAYWRIGHT_CONTEXT

                if _PLAYWRIGHT_CONTEXT is not None and _PLAYWRIGHT_HEADLESS != headless:
                    close_browser_session()

                _PLAYWRIGHT_INSTANCE = sync_playwright().start()
                _PLAYWRIGHT_BROWSER = _launch_browser(_PLAYWRIGHT_INSTANCE, headless)
                _PLAYWRIGHT_CONTEXT = _PLAYWRIGHT_BROWSER.new_context(
                    locale="en-US",
                    user_agent=random.choice(_USER_AGENTS),
                    viewport={"width": 1440, "height": 1080},
                )
                _PLAYWRIGHT_HEADLESS = headless
                _PLAYWRIGHT_OWNER_THREAD_ID = threading.get_ident()
                logger.info("Started persistent browser session for fallback scraping")
                return _PLAYWRIGHT_CONTEXT


def _can_reuse_persistent_browser_on_current_thread() -> bool:
                """Return True only when current thread owns the persistent Playwright objects."""
                if _PLAYWRIGHT_CONTEXT is None:
                    return True

                current_thread_id = threading.get_ident()
                if _PLAYWRIGHT_OWNER_THREAD_ID in (None, current_thread_id):
                    return True

                logger.debug(
                    "Skipping persistent browser reuse on thread %s; owner thread is %s",
                    current_thread_id,
                    _PLAYWRIGHT_OWNER_THREAD_ID,
                )
                return False


def _get_or_create_persistent_page(headless: bool):
                """Create or return a persistent browser page reused across rows."""
                global _PLAYWRIGHT_PAGE

                context = _get_or_create_persistent_context(headless=headless)

                if _PLAYWRIGHT_PAGE is not None:
                    try:
                        if not _PLAYWRIGHT_PAGE.is_closed():
                            return _PLAYWRIGHT_PAGE
                    except Exception:
                        pass
                    _PLAYWRIGHT_PAGE = None

                for existing_page in context.pages:
                    try:
                        if not existing_page.is_closed():
                            _PLAYWRIGHT_PAGE = existing_page
                            return _PLAYWRIGHT_PAGE
                    except Exception:
                        continue

                _PLAYWRIGHT_PAGE = context.new_page()
                return _PLAYWRIGHT_PAGE


def _register_captcha_or_block_event(source: str, base_seconds: int, max_seconds: int) -> None:
                """Increase adaptive cooldown after a CAPTCHA/block event."""
                global _CAPTCHA_EVENT_COUNT, _CAPTCHA_COOLDOWN_UNTIL

                with _STATE_LOCK:
                    _CAPTCHA_EVENT_COUNT += 1
                    adaptive_max = min(max_seconds, max(base_seconds, base_seconds * _CAPTCHA_EVENT_COUNT))
                    cooldown = random.randint(base_seconds, adaptive_max)
                    _CAPTCHA_COOLDOWN_UNTIL = max(_CAPTCHA_COOLDOWN_UNTIL, time.time() + float(cooldown))
                logger.warning(
                    "Adaptive cooldown triggered by %s: %ds (event #%d)",
                    source,
                    cooldown,
                    _CAPTCHA_EVENT_COUNT,
                )


def _apply_adaptive_cooldown() -> None:
                """Sleep if there is an active adaptive cooldown window."""
                with _STATE_LOCK:
                    remaining = _CAPTCHA_COOLDOWN_UNTIL - time.time()
                if remaining > 0:
                    logger.info("Cooling down for %.1fs to reduce CAPTCHA risk", remaining)
                    time.sleep(remaining)


def _launch_browser(playwright: Any, headless: bool):
                """Launch an installed browser channel, preferring system Chrome/Edge."""
                launch_attempts: list[tuple[Any, dict[str, Any]]] = []

                if sys.platform.startswith("win"):
                    launch_attempts.extend(
                        [
                            (playwright.chromium, {"channel": "msedge", "headless": headless}),
                            (playwright.chromium, {"channel": "chrome", "headless": headless}),
                        ]
                    )
                else:
                    launch_attempts.append((playwright.chromium, {"channel": "chrome", "headless": headless}))

                launch_attempts.extend(
                    [
                        (playwright.chromium, {"headless": headless}),
                        (playwright.firefox, {"headless": headless}),
                    ]
                )

                last_error: Optional[Exception] = None
                for browser_type, kwargs in launch_attempts:
                    try:
                        return browser_type.launch(**kwargs)
                    except PlaywrightError as exc:
                        last_error = exc
                        logger.debug("Browser launch attempt failed with %s: %s", kwargs, exc)

                raise PlaywrightError(str(last_error or "No Playwright browser could be launched"))


def _has_captcha(html: str, page: Any) -> bool:
    """Detect if the current page is showing an active Google CAPTCHA."""
    _ = html
    active_captcha_markers = [
        'iframe[src*="recaptcha"]',
        'div[data-recaptchaid]',
        ".g-recaptcha",
    ]

    try:
        for selector in active_captcha_markers:
            try:
                if page.locator(selector).count() > 0:
                    logger.debug("Active CAPTCHA element detected: %s", selector)
                    return True
            except PlaywrightError:
                continue
    except Exception:
        pass

    try:
        title = page.title().lower()
        if "verify" in title and "robot" in title:
            return True
    except PlaywrightError:
        pass

    return False


def _wait_for_captcha_clear(page: Any, timeout_seconds: int) -> bool:
    """Poll for CAPTCHA to be cleared and page to load real images."""
    timeout_ms = timeout_seconds * 1000
    poll_interval_ms = 1000
    elapsed = 0
    stable_clear_count = 0

    while elapsed < timeout_ms:
        try:
            has_active_captcha = _has_captcha(page.content(), page)
            if not has_active_captcha:
                stable_clear_count += 1
            else:
                stable_clear_count = 0

            try:
                img_count = page.locator("img").count()
            except PlaywrightError:
                img_count = 0

            if img_count >= 10 and stable_clear_count >= 1:
                logger.info("CAPTCHA cleared: %d images + no active challenge", img_count)
                return True

            if img_count >= 5 and stable_clear_count >= 2:
                logger.info("CAPTCHA cleared: %d images + confirmed no active challenge", img_count)
                return True

            try:
                current_url = str(page.url).lower()
                if "answers.google.com" not in current_url and "challenge" not in current_url and img_count >= 3:
                    logger.info("CAPTCHA cleared by URL+image heuristic: %s", current_url)
                    return True
            except Exception:
                pass

            page.wait_for_timeout(min(poll_interval_ms, timeout_ms - elapsed))
            elapsed += poll_interval_ms
        except Exception as exc:
            logger.debug("Poll error: %s", exc)
            page.wait_for_timeout(500)
            elapsed += 500

    logger.warning("CAPTCHA clear timeout after %ss", timeout_seconds)
    return False


def _wait_for_images_to_render(page: Any, timeout_ms: int = 10_000) -> int:
    """Wait for images to render after CAPTCHA is solved."""
    poll_interval = 500
    elapsed = 0
    last_count = 0

    while elapsed < timeout_ms:
        try:
            img_count = page.locator("img").count()
            if img_count > last_count:
                logger.debug("Images rendering: %d found (previous %d)", img_count, last_count)
                last_count = img_count

            if img_count > 10:
                page.wait_for_timeout(500)
                final_count = page.locator("img").count()
                if final_count >= img_count:
                    logger.info("Images stable: %d images loaded in DOM", final_count)
                    return final_count

            page.wait_for_timeout(poll_interval)
            elapsed += poll_interval
        except PlaywrightError as exc:
            logger.debug("Image count check failed: %s", exc)
            page.wait_for_timeout(poll_interval)
            elapsed += poll_interval

    logger.info("Image render timeout; found %d images in final state", last_count)
    return last_count


def _decode_data_image_url(data_url: str, min_image_bytes: int) -> Optional[bytes]:
    """Decode a data:image URL into bytes if it looks plausible."""
    if "," not in data_url:
        return None

    _, b64_data = data_url.split(",", 1)
    b64_data = _normalize_b64_string(b64_data)
    padding = (4 - len(b64_data) % 4) % 4
    b64_data += "=" * padding

    try:
        raw = base64.b64decode(b64_data)
    except Exception:
        return None

    if min_image_bytes <= len(raw) <= 6_000_000:
        return raw
    return None


def _extract_browser_image_sources(page: Any) -> list[str]:
    """Read candidate image sources from a rendered Google Images page."""
    try:
        sources = page.evaluate(
            """
            () => Array.from(document.images)
                .map((img) => ({
                    src: img.currentSrc || img.src || '',
                    width: img.naturalWidth || img.width || 0,
                    height: img.naturalHeight || img.height || 0,
                }))
                .filter((img) => img.src)
                .filter((img) => img.width >= 80 && img.height >= 80)
                .filter((img) => !img.src.includes('google.com/images/branding'))
                .map((img) => img.src)
            """
        )
    except PlaywrightError as exc:
        logger.debug("Could not inspect browser image sources: %s", exc)
        return []

    deduped: list[str] = []
    seen: set[str] = set()
    for src in sources or []:
        if src and src not in seen:
            seen.add(src)
            deduped.append(src)
    return deduped


def _screenshot_first_result_image(page: Any, min_image_bytes: int, result_index: int = 1) -> Optional[bytes]:
    """Capture a plausible visible image result as a PNG screenshot.
    
    Args:
        page: Playwright page object.
        min_image_bytes: Minimum image size in bytes.
        result_index: Which result to capture (1-indexed). 1 = first, 2 = second, etc.
    """
    try:
        image_elements = page.locator("img")
        count = min(image_elements.count(), 20)
        logger.info("[DEBUG] _screenshot_first_result_image: total img elements on page: %d, target result_index: %d", count, result_index)
    except PlaywrightError as exc:
        logger.debug("Could not enumerate browser images: %s", exc)
        return None

    found_count = 0
    skipped_count = 0
    small_box_count = 0
    
    for index in range(count):
        try:
            locator = image_elements.nth(index)
            src = locator.get_attribute("src") or locator.get_attribute("currentSrc") or ""
            
            # Skip branding
            if "google.com/images/branding" in src:
                skipped_count += 1
                logger.debug("[DEBUG] img[%d] skipped: branding", index)
                continue

            # Check size
            box = locator.bounding_box()
            if not box or box["width"] < 80 or box["height"] < 80:
                small_box_count += 1
                logger.debug("[DEBUG] img[%d] skipped: small/no box (w=%s h=%s)", index, box.get("width") if box else "None", box.get("height") if box else "None")
                continue

            # Try to screenshot
            image_bytes = locator.screenshot(type="png")
            if len(image_bytes) >= min_image_bytes:
                found_count += 1
                logger.info("[DEBUG] img[%d] valid result #%d (%d bytes, w=%d h=%d)", index, found_count, len(image_bytes), box["width"], box["height"])
                
                if found_count == result_index:
                    logger.info("Captured screenshot result #%d at element index %d (%d bytes)", result_index, index, len(image_bytes))
                    return image_bytes
            else:
                logger.debug("[DEBUG] img[%d] too small (%d bytes < %d min)", index, len(image_bytes), min_image_bytes)
                
        except PlaywrightError as exc:
            logger.debug("[DEBUG] img[%d] error: %s", index, exc)
            continue

    logger.warning("[DEBUG] Could not capture image result #%d - scanned %d elements, %d branding, %d small_box, %d valid results found", 
                   result_index, count, skipped_count, small_box_count, found_count)
    return None


def _rotate_session() -> None:
    """Recreate the current thread session after blocking/interstitials."""
    session = requests.Session()
    session.mount("http://", _ADAPTER)
    session.mount("https://", _ADAPTER)
    _THREAD_STATE.session = session


def _get_thread_session() -> requests.Session:
    """Return a per-thread HTTP session so concurrent workers do not share state."""
    session = getattr(_THREAD_STATE, "session", None)
    if session is None:
        session = requests.Session()
        session.mount("http://", _ADAPTER)
        session.mount("https://", _ADAPTER)
        _THREAD_STATE.session = session
    return session


def _sanitize_query_part(part: str) -> str:
    """Remove punctuation that hurts matching and normalize whitespace."""
    cleaned = _STRIP_QUERY_CHARS.sub(" ", part or "")
    return " ".join(cleaned.split())
