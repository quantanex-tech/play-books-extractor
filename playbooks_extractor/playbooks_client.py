"""Google Play Books discovery and audio/PDF export adapter (HTTP + Headless Playwright)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from playbooks_extractor.config import Settings
from playbooks_extractor.cookies import CookieStore
from playbooks_extractor.storage import (
    STANDARD_AUDIO_EXTENSIONS,
    has_mp4_drm_encryption,
    is_valid_audio_file,
    is_valid_pdf_file,
)

ProgressCallback = Callable[[int, int, str], Awaitable[None]]

VOLUME_ID_QUERY_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]{4,64})")
AF_CALLBACK_RE = re.compile(
    r"AF_initDataCallback\(\s*\{.*?\bdata\s*:\s*(\[.*?\])\s*(?:,\s*sideChannel|\}\s*\))",
    re.DOTALL,
)
VIDEOPLAYBACK_URL_RE = re.compile(
    r"https?://[A-Za-z0-9._:-]+/videoplayback\?[^\"'\s<>\\]+"
)
_STRIP_PLAYBACK_PARAMS = frozenset({"range", "alr", "ump", "srfvp", "rn", "rbuf"})


class DrmProtectedError(RuntimeError):
    """Raised when an audio stream is encrypted with CENC/DRM rather than playable audio."""


def normalize_videoplayback_url(raw_url: str) -> str:
    """Strip partial-range/UMP streaming parameters from a videoplayback URL so GET returns the full file."""
    parsed = urlparse(raw_url.strip())
    filtered_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k not in _STRIP_PLAYBACK_PARAMS
    ]
    return urlunparse(parsed._replace(query=urlencode(filtered_pairs, doseq=True)))


def rank_videoplayback_urls(urls: list[str], *, prefer_high: bool = True) -> str:
    """Choose the highest-quality audio stream URL from candidate videoplayback URLs."""
    if not urls:
        return ""
    itag_scores = (
        {"141": 300, "140": 200, "251": 190, "250": 150, "249": 140, "139": 100}
        if prefer_high
        else {"140": 300, "141": 200, "251": 190, "250": 150, "249": 140, "139": 100}
    )
    best_url = ""
    best_key = (-1, -1)
    for raw in urls:
        cleaned = normalize_videoplayback_url(raw)
        qs = parse_qs(urlparse(cleaned).query)
        itag = (qs.get("itag") or [""])[0].strip()
        raw_clen = (qs.get("clen") or ["0"])[0].strip()
        clen = int(raw_clen) if raw_clen.isdigit() else 0
        score = itag_scores.get(itag, 120 if itag else 50)
        key = (score, clen)
        if key > best_key:
            best_key = key
            best_url = cleaned
    return best_url


def extract_best_videoplayback_url(raw_text: str, *, prefer_high: bool = True) -> str:
    """Unescape Google JS/HTML string literals and select the best videoplayback audio stream URL."""
    if not raw_text or "videoplayback" not in raw_text:
        return ""
    unescaped = (
        raw_text.replace("\\\\u003d", "=")
        .replace("\\u003d", "=")
        .replace("\\\\u0026amp;", "&")
        .replace("\\u0026amp;", "&")
        .replace("\\\\u0026", "&")
        .replace("\\u0026", "&")
        .replace("\\x3d", "=")
        .replace("\\x26", "&")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )
    candidates = VIDEOPLAYBACK_URL_RE.findall(unescaped)
    return rank_videoplayback_urls(candidates, prefer_high=prefer_high)


@dataclass
class ResolvedExportTargets:
    """Resolved download URLs or browser-downloaded temp artifacts for an audiobook."""

    audio_url: str = ""
    pdf_url: str = ""
    export_supported: bool = True
    pre_downloaded_audio: Path | None = None
    pre_downloaded_pdf: Path | None = None
    audio_ext_hint: str = ".m4a"
    note: str = ""


class PlayBooksClient:
    """Adapter for discovering audiobooks and exporting .m4a + accompanying .pdf from Google Play Books."""

    def __init__(
        self,
        settings: Settings,
        cookie_store: CookieStore,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.cookie_store = cookie_store
        self.transport = transport

    def _require_cookies(self) -> str:
        header = self.cookie_store.build_cookie_header()
        if not header:
            raise PermissionError(
                "No Google Play Books session cookies imported. Import cookies in the Authentication panel first."
            )
        return header

    def _default_headers(self, cookie_header: str) -> dict[str, str]:
        return {
            "User-Agent": self.settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": cookie_header,
        }

    @staticmethod
    def _is_login_redirect(url_str: str, status_code: int) -> bool:
        lower = url_str.lower()
        host = (urlparse(url_str).hostname or "").lower()
        return (
            status_code in (401, 403)
            or "accounts.google.com" in host
            or "/servicelogin" in lower
            or "/signin" in lower
        )

    async def discover_audiobooks(self) -> list[dict[str, Any]]:
        """Discover purchased audiobooks from Google Play Books using HTTP/DOM + headless browser fallback."""
        cookie_header = self._require_cookies()
        books_by_id: dict[str, dict[str, Any]] = {}

        if self.settings.use_browser != "always":
            http_books = await self._discover_via_http(cookie_header)
            for b in http_books:
                books_by_id[b["volume_id"]] = b

        should_try_browser = (
            self.settings.use_browser == "always"
            or (self.settings.use_browser == "auto" and not books_by_id and self.transport is None)
        )
        if should_try_browser:
            browser_books = await self._discover_via_playwright()
            for b in browser_books:
                existing = books_by_id.get(b["volume_id"], {})
                merged = {**existing, **{k: v for k, v in b.items() if v not in ("", None)}}
                books_by_id[b["volume_id"]] = merged

        books = list(books_by_id.values())
        await self._enrich_missing_metadata(books, cookie_header)
        return books

    async def _discover_via_http(self, cookie_header: str) -> list[dict[str, Any]]:
        base = self.settings.base_url.rstrip("/")
        candidate_urls = [f"{base}/books/audiobooks", f"{base}/books"]
        headers = self._default_headers(cookie_header)
        discovered: dict[str, dict[str, Any]] = {}

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=25.0,
            transport=self.transport,
        ) as client:
            for url in candidate_urls:
                try:
                    resp = await client.get(url, headers=headers)
                except httpx.HTTPError as exc:
                    if url == candidate_urls[-1] and not discovered:
                        raise RuntimeError(f"Failed reaching Google Play Books ({url}): {exc}") from exc
                    continue

                if self._is_login_redirect(str(resp.url), resp.status_code):
                    raise PermissionError(
                        "Google Play Books session redirected to Sign-In. Please import fresh cookies."
                    )

                if resp.status_code >= 400:
                    continue

                parsed_items = self.parse_library_html(resp.text, base_url=str(resp.url))
                for item in parsed_items:
                    vid = item["volume_id"]
                    if vid not in discovered:
                        discovered[vid] = item
                    else:
                        for k, v in item.items():
                            if v and not discovered[vid].get(k):
                                discovered[vid][k] = v

                if discovered:
                    break

        return list(discovered.values())

    def parse_library_html(self, html: str, base_url: str) -> list[dict[str, Any]]:
        """Extract audiobook records from Google Play Books HTML DOM and AF_initDataCallback blocks."""
        soup = BeautifulSoup(html, "html.parser")
        books: dict[str, dict[str, Any]] = {}

        # 1. Explicit data-volume-id cards/elements
        for card in soup.select("[data-volume-id]"):
            vid = (card.get("data-volume-id") or "").strip()
            if not vid:
                continue
            record = self._extract_card_record(card, vid, base_url)
            books[vid] = self._merge_book_dict(books.get(vid), record)

        # 2. Anchor links referencing audiobook / reader / details volume IDs (?id=<VolumeID>)
        for link in soup.find_all("a", href=True):
            href = str(link["href"])
            if not self._is_candidate_book_href(href):
                continue
            vid = self._extract_volume_id_from_url(href)
            if not vid:
                continue
            # Climb to enclosing card container (preferring <gpb-volume-card> / .card over inner .metadata divs)
            container = self._find_enclosing_card_container(link)
            record = self._extract_card_record(container, vid, base_url, primary_link=link)
            books[vid] = self._merge_book_dict(books.get(vid), record)

        # 3. Parse AF_initDataCallback embedded JSON structures from Google Wiz SSR
        for match in AF_CALLBACK_RE.finditer(html):
            raw_json = match.group(1)
            try:
                data = json.loads(raw_json)
            except json.JSONDecodeError:
                continue
            for af_book in self._walk_af_init_data(data, base_url):
                vid = af_book["volume_id"]
                books[vid] = self._merge_book_dict(books.get(vid), af_book)

        return list(books.values())

    def _extract_card_record(
        self,
        container: Any,
        volume_id: str,
        base_url: str,
        primary_link: Any | None = None,
    ) -> dict[str, Any]:
        title = ""
        authors = ""
        cover_url = ""
        export_url = ""
        pdf_url = ""
        # Every discovered audiobook remains eligible for export (via UI Export or /books/listen player fallback)
        export_supported = True

        if container.get("data-title"):
            title = str(container["data-title"]).strip()
        if container.get("data-authors") or container.get("data-author"):
            authors = str(container.get("data-authors") or container.get("data-author") or "").strip()
        if container.get("data-export-url"):
            export_url = urljoin(base_url, str(container["data-export-url"]).strip())
        if container.get("data-pdf-url"):
            pdf_url = urljoin(base_url, str(container["data-pdf-url"]).strip())

        if not title:
            card_link_el = container.select_one("a.card-link[title]")
            if card_link_el and card_link_el.get("title"):
                title = str(card_link_el["title"]).strip()
        if not title:
            title_el = container.select_one(
                "[data-book-title], .book-title, .title, h2, h3, h4, [role='heading']"
            )
            if title_el:
                title = title_el.get_text(" ", strip=True)
        if not title and primary_link is not None:
            title = (
                str(primary_link.get("aria-label") or primary_link.get("title") or "").strip()
                or primary_link.get_text(" ", strip=True)
            )
        if not title and container.get("aria-label"):
            title = str(container.get("aria-label")).strip()

        if not authors:
            author_el = container.select_one(
                "[data-book-author], .book-author, .author, .subtitle, a[href*='/books/author']"
            )
            if author_el:
                authors = author_el.get_text(" ", strip=True)

        img = container.find("img")
        if img and (img.get("src") or img.get("data-src")):
            cover_url = urljoin(base_url, str(img.get("src") or img.get("data-src")))

        for a_tag in container.find_all("a", href=True):
            href_abs = urljoin(base_url, str(a_tag["href"]))
            label = (
                f"{a_tag.get_text(' ', strip=True)} "
                f"{a_tag.get('aria-label', '')} "
                f"{a_tag.get('data-action', '')}"
            ).lower()
            href_lower = href_abs.lower()
            if (
                "pdf" in label
                or "supplement" in label
                or href_lower.endswith(".pdf")
                or "output=pdf" in href_lower
                or "format=pdf" in href_lower
            ):
                if not pdf_url:
                    pdf_url = href_abs
            elif (
                "export" in label
                or "download audio" in label
                or "m4a" in label
                or href_lower.endswith(".m4a")
                or "output=m4a" in href_lower
                or "format=m4a" in href_lower
                or ("/download" in href_lower and "pdf" not in href_lower)
            ):
                if not export_url:
                    export_url = href_abs

        return {
            "volume_id": volume_id,
            "title": title or f"Audiobook {volume_id}",
            "authors": authors,
            "cover_url": cover_url,
            "export_url": export_url,
            "pdf_url": pdf_url,
            "has_pdf": bool(pdf_url),
            "export_supported": export_supported,
        }

    def _walk_af_init_data(self, node: Any, base_url: str) -> list[dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}

        def visit(item: Any) -> None:
            if isinstance(item, list):
                # Look for list tuples where first or early element is a 12-char volume ID
                # accompanied by a cover/books URL or export URL in the same tuple
                strings = [x for x in item if isinstance(x, str)]
                joined = " ".join(strings)
                if (
                    len(strings) >= 2
                    and re.fullmatch(r"[A-Za-z0-9_-]{8,24}", strings[0])
                    and (
                        "books.google" in joined
                        or "googleusercontent.com" in joined
                        or "/books/" in joined
                        or "audiobook" in joined.lower()
                    )
                ):
                    vid = strings[0]
                    title_candidates = [
                        s
                        for s in strings[1:]
                        if not s.startswith(("http://", "https://", "/"))
                        and len(s) > 1
                        and not re.fullmatch(r"[A-Za-z0-9_-]{12}", s)
                    ]
                    cover = next(
                        (s for s in strings if s.startswith("http") and ("content" in s or "googleusercontent" in s)),
                        "",
                    )
                    export_link = next(
                        (
                            urljoin(base_url, s)
                            for s in strings
                            if ("download" in s.lower() or "export" in s.lower() or ".m4a" in s.lower())
                            and (s.startswith(("http://", "https://", "/")))
                        ),
                        "",
                    )
                    pdf_link = next(
                        (
                            urljoin(base_url, s)
                            for s in strings
                            if ("pdf" in s.lower()) and (s.startswith(("http://", "https://", "/")))
                        ),
                        "",
                    )
                    if title_candidates:
                        results[vid] = self._merge_book_dict(
                            results.get(vid),
                            {
                                "volume_id": vid,
                                "title": title_candidates[0],
                                "authors": title_candidates[1] if len(title_candidates) > 1 else "",
                                "cover_url": cover,
                                "export_url": export_link,
                                "pdf_url": pdf_link,
                                "has_pdf": bool(pdf_link),
                                "export_supported": True,
                            },
                        )
                for sub in item:
                    visit(sub)
            elif isinstance(item, dict):
                for val in item.values():
                    visit(val)

        visit(node)
        return list(results.values())

    @staticmethod
    def _is_candidate_book_href(href: str) -> bool:
        lower = href.lower()
        if any(
            bad in lower
            for bad in (
                "/books/author",
                "/store/books/author",
                "/books/collection",
                "/store/books/collection",
                "/books/category",
                "/store/books/category",
                "/books/publisher",
                "/store/books/publisher",
                "accounts.google.com",
            )
        ):
            return False
        return any(
            seg in lower
            for seg in (
                "/books/listen",
                "/books/ab",
                "/books/reader",
                "/books/details",
                "/store/books/details",
            )
        ) or (lower.startswith("?") and "id=" in lower)

    @staticmethod
    def _find_enclosing_card_container(link: Any) -> Any:
        explicit = link.find_parent(["gpb-volume-card", "article", "li"])
        if explicit is not None:
            return explicit
        card_div = link.find_parent(
            "div",
            class_=lambda classes: bool(
                classes
                and any(
                    "card" in token.lower()
                    for token in (classes if isinstance(classes, list) else str(classes).split())
                )
            ),
        )
        if card_div is not None:
            return card_div
        return link.find_parent("div") or link

    @staticmethod
    def _extract_volume_id_from_url(url_str: str) -> str:
        parsed = urlparse(url_str)
        path_low = (parsed.path or "").lower()
        if any(
            bad in path_low
            for bad in ("/author", "/collection", "/category", "/publisher", "/series")
        ):
            return ""
        qs = parse_qs(parsed.query)
        if "id" in qs:
            cand = (qs["id"][0] if qs["id"] else "").strip()
            return cand if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", cand) else ""
        match = VOLUME_ID_QUERY_RE.search(url_str)
        return match.group(1) if match else ""

    @staticmethod
    def _merge_book_dict(
        existing: dict[str, Any] | None, incoming: dict[str, Any]
    ) -> dict[str, Any]:
        if not existing:
            return dict(incoming)
        merged = dict(existing)
        for key, val in incoming.items():
            if key == "title":
                cur_title = str(merged.get("title") or "")
                new_title = str(val or "")
                if (not cur_title or cur_title.startswith("Audiobook ")) and new_title:
                    merged["title"] = new_title
            elif key == "has_pdf":
                merged["has_pdf"] = bool(merged.get("has_pdf") or val)
            elif key == "export_supported":
                merged["export_supported"] = bool(merged.get("export_supported", True) and val)
            elif val and not merged.get(key):
                merged[key] = val
        return merged

    async def _enrich_missing_metadata(
        self, books: list[dict[str, Any]], cookie_header: str
    ) -> None:
        if not self.settings.books_api_url or not books:
            return
        needs_enrichment = [
            b
            for b in books
            if not b.get("title")
            or str(b.get("title", "")).startswith("Audiobook ")
        ]
        if not needs_enrichment:
            return

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=8.0,
            transport=self.transport,
        ) as client:
            for book in needs_enrichment[:25]:
                vid = book["volume_id"]
                url = f"{self.settings.books_api_url.rstrip('/')}/{vid}"
                try:
                    resp = await client.get(url, headers={"User-Agent": self.settings.user_agent, "Cookie": cookie_header})
                    if resp.status_code != 200:
                        continue
                    info = resp.json().get("volumeInfo", {})
                    if info.get("title"):
                        book["title"] = str(info["title"]).strip()
                    if info.get("authors") and not book.get("authors"):
                        book["authors"] = ", ".join(str(a) for a in info["authors"])
                    thumbs = info.get("imageLinks") or {}
                    if not book.get("cover_url") and (thumbs.get("thumbnail") or thumbs.get("smallThumbnail")):
                        book["cover_url"] = str(thumbs.get("thumbnail") or thumbs.get("smallThumbnail"))
                except Exception:
                    continue

    async def _discover_via_playwright(self) -> list[dict[str, Any]]:
        """Render Google Play Books SPA in headless Chromium to discover lazy-loaded audiobooks."""
        from playwright.async_api import async_playwright

        cookies, _ = self.cookie_store.load()
        if not cookies:
            raise PermissionError("No Google Play Books session cookies imported.")

        base = self.settings.base_url.rstrip("/")
        pw_cookies = [c.to_playwright(base) for c in cookies]

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(user_agent=self.settings.user_agent)
                await context.add_cookies(pw_cookies)
                page = await context.new_page()

                await page.goto(f"{base}/books/audiobooks", wait_until="domcontentloaded", timeout=30000)
                if self._is_login_redirect(page.url, 200):
                    raise PermissionError(
                        "Google Play Books redirected to Sign-In. Please re-import valid session cookies."
                    )

                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                # Scroll iteratively to load virtualized/lazy book cards
                last_height = 0
                for _ in range(6):
                    height = await page.evaluate("document.body ? document.body.scrollHeight : 0")
                    if height == last_height:
                        break
                    last_height = height
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(500)

                html = await page.content()
                return self.parse_library_html(html, base_url=page.url)
            finally:
                await browser.close()

    async def resolve_export_targets(
        self,
        book: dict[str, Any],
        book_folder: Path,
        progress_cb: ProgressCallback | None = None,
    ) -> ResolvedExportTargets:
        """Resolve audio (.m4a) and optional accompanying PDF (.pdf) download URLs or browser downloads."""
        cookie_header = self._require_cookies()
        volume_id = book["volume_id"]
        audio_url = str(book.get("export_url") or "").strip()
        pdf_url = str(book.get("pdf_url") or "").strip()
        ui_export_disabled = False

        # 1. If export_url or pdf_url missing, inspect the book's detail/audiobook page via HTTP first
        if (not audio_url or not pdf_url) and self.settings.use_browser != "always":
            probed_audio, probed_pdf, supported = await self._probe_book_detail_http(
                volume_id=volume_id,
                cookie_header=cookie_header,
            )
            ui_export_disabled = not supported
            audio_url = audio_url or probed_audio
            pdf_url = pdf_url or probed_pdf

        if audio_url:
            return ResolvedExportTargets(
                audio_url=audio_url,
                pdf_url=pdf_url,
                export_supported=True,
            )

        # 2. Use headless Playwright Chromium to click the book's More menu -> Export (high) / Download PDF,
        #    falling back automatically to the /books/listen web player stream when UI Export is absent.
        if self.settings.use_browser != "never":
            return await self._resolve_or_download_via_playwright(
                book=book,
                book_folder=book_folder,
                progress_cb=progress_cb,
                cookie_header=cookie_header,
            )

        # 3. HTTP-only mode (use_browser == 'never'): extract unencrypted audio stream from /books/listen
        player_audio_url = await self._probe_listen_player_http(
            volume_id=volume_id,
            cookie_header=cookie_header,
        )
        if player_audio_url:
            return ResolvedExportTargets(
                audio_url=player_audio_url,
                pdf_url=pdf_url,
                export_supported=True,
                audio_ext_hint=".m4a",
                note="Extracted directly from Google Play Books web player stream.",
            )

        if ui_export_disabled:
            return ResolvedExportTargets(
                export_supported=False,
                note="No Export option or unencrypted web-player audio stream available for this title.",
            )

        raise RuntimeError(
            f"Could not find an export link or web-player audio stream for '{book.get('title', volume_id)}' ({volume_id}). "
            "Session cookies may need refreshing."
        )

    async def _probe_listen_player_http(self, volume_id: str, cookie_header: str) -> str:
        """Extract the highest-quality unencrypted audio/mp4 stream URL from /books/listen?id=<volume_id>."""
        base = self.settings.base_url.rstrip("/")
        url = f"{base}/books/listen?id={volume_id}"
        headers = self._default_headers(cookie_header)
        prefer_high = self.settings.export_quality != "standard"

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            transport=self.transport,
        ) as client:
            try:
                resp = await client.get(url, headers=headers)
            except httpx.HTTPError:
                return ""
            if self._is_login_redirect(str(resp.url), resp.status_code):
                raise PermissionError("Session expired while resolving audiobook web player stream.")
            if resp.status_code >= 400:
                return ""

            best = extract_best_videoplayback_url(resp.text, prefer_high=prefer_high)
            if best:
                return best

            soup = BeautifulSoup(resp.text, "html.parser")
            for media_el in soup.select("audio[src], audio source[src]"):
                src = str(media_el.get("src") or "").strip()
                if src:
                    abs_src = urljoin(str(resp.url), src)
                    return (
                        normalize_videoplayback_url(abs_src)
                        if "/videoplayback?" in abs_src
                        else abs_src
                    )
        return ""

    async def _capture_listen_player_via_playwright(self, page: Any, volume_id: str) -> str:
        """Navigate Playwright to /books/listen?id=<volume_id> and capture the audio/mp4 stream URL."""
        base = self.settings.base_url.rstrip("/")
        prefer_high = self.settings.export_quality != "standard"
        captured_urls: list[str] = []

        def on_request(req: Any) -> None:
            try:
                u = str(req.url or "")
                if "/videoplayback?" in u:
                    captured_urls.append(u)
            except Exception:
                pass

        def on_response(resp: Any) -> None:
            try:
                u = str(resp.url or "")
                ct = (resp.headers.get("content-type") or "").lower()
                if "/videoplayback?" in u or ct.startswith("audio/"):
                    captured_urls.append(u)
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)
        try:
            await page.goto(
                f"{base}/books/listen?id={volume_id}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            if self._is_login_redirect(page.url, 200):
                raise PermissionError(
                    "Google Play Books web player redirected to Sign-In; re-import fresh cookies."
                )

            for _ in range(12):
                if captured_urls:
                    await page.wait_for_timeout(500)
                    break
                await page.wait_for_timeout(500)

            html_best = extract_best_videoplayback_url(await page.content(), prefer_high=prefer_high)
            if html_best:
                captured_urls.append(html_best)

            if captured_urls:
                return rank_videoplayback_urls(captured_urls, prefer_high=prefer_high)

            audio_src = await page.evaluate(
                """() => {
                    const el = document.querySelector('audio[src], audio source[src]');
                    return el ? (el.currentSrc || el.getAttribute('src') || '') : '';
                }"""
            )
            if audio_src:
                abs_src = urljoin(page.url, str(audio_src))
                return (
                    normalize_videoplayback_url(abs_src)
                    if "/videoplayback?" in abs_src
                    else abs_src
                )
            return ""
        finally:
            try:
                page.remove_listener("request", on_request)
                page.remove_listener("response", on_response)
            except Exception:
                pass

    async def _probe_book_detail_http(
        self, volume_id: str, cookie_header: str
    ) -> tuple[str, str, bool]:
        base = self.settings.base_url.rstrip("/")
        headers = self._default_headers(cookie_header)
        detail_urls = [
            f"{base}/books/ab?id={volume_id}",
            f"{base}/books/reader?id={volume_id}",
        ]
        audio_url = ""
        pdf_url = ""

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=20.0,
            transport=self.transport,
        ) as client:
            for url in detail_urls:
                try:
                    resp = await client.get(url, headers=headers)
                except httpx.HTTPError:
                    continue
                if self._is_login_redirect(str(resp.url), resp.status_code):
                    raise PermissionError("Session expired while resolving audiobook export links.")
                if resp.status_code >= 400:
                    continue
                records = self.parse_library_html(resp.text, base_url=str(resp.url))
                for rec in records:
                    if rec["volume_id"] == volume_id:
                        if rec.get("export_supported") is False:
                            return "", "", False
                        audio_url = audio_url or rec.get("export_url", "")
                        pdf_url = pdf_url or rec.get("pdf_url", "")
                # Also check direct links on the detail page dedicated to this volume
                soup = BeautifulSoup(resp.text, "html.parser")
                if soup.select_one("[data-export-supported='false']"):
                    return "", "", False
                for a_tag in soup.find_all("a", href=True):
                    href_abs = urljoin(str(resp.url), str(a_tag["href"]))
                    label = f"{a_tag.get_text(' ', strip=True)} {a_tag.get('aria-label', '')}".lower()
                    href_low = href_abs.lower()
                    if ("pdf" in label or "supplement" in label or href_low.endswith(".pdf") or "output=pdf" in href_low) and not pdf_url:
                        pdf_url = href_abs
                    elif ("export" in label or "m4a" in label or href_low.endswith(".m4a") or "output=m4a" in href_low) and not audio_url:
                        audio_url = href_abs
                if audio_url:
                    break

        return audio_url, pdf_url, True

    async def _resolve_or_download_via_playwright(
        self,
        book: dict[str, Any],
        book_folder: Path,
        progress_cb: ProgressCallback | None = None,
        cookie_header: str = "",
    ) -> ResolvedExportTargets:
        """Click the audiobook's More menu in headless Chromium to trigger/capture audio & PDF exports."""
        from playwright.async_api import async_playwright

        cookies, _ = self.cookie_store.load()
        if not cookies:
            raise PermissionError("No Google Play Books session cookies imported.")

        volume_id = book["volume_id"]
        base = self.settings.base_url.rstrip("/")
        pw_cookies = [c.to_playwright(base) for c in cookies]

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    accept_downloads=True,
                    user_agent=self.settings.user_agent,
                )
                await context.add_cookies(pw_cookies)
                page = await context.new_page()

                response_lengths: dict[str, int] = {}

                def on_response(response: Any) -> None:
                    try:
                        cl = response.headers.get("content-length")
                        if cl and cl.isdigit():
                            response_lengths[response.url] = int(cl)
                    except Exception:
                        pass

                page.on("response", on_response)

                await page.goto(f"{base}/books/audiobooks", wait_until="domcontentloaded", timeout=30000)
                if self._is_login_redirect(page.url, 200):
                    raise PermissionError("Google Play Books redirected to Sign-In; re-import fresh cookies.")

                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass

                # Locate the specific card referencing volume_id (scrolling if virtualized/lazy)
                card = await self._locate_book_card_in_playwright(page, volume_id)
                menu_opened = False
                if card is not None:
                    menu_opened = await self._click_card_more_menu(card, page)

                targets = ResolvedExportTargets()

                # Classify visible menu items / export actions cleanly into Audio vs PDF
                audio_item, pdf_item = await self._classify_export_clickables(
                    page, volume_id, menu_opened=menu_opened
                )
                had_pdf_item = pdf_item is not None

                if audio_item is not None:
                    href = await audio_item.get_attribute("href")
                    if href and not href.strip().lower().startswith("javascript:"):
                        targets.audio_url = urljoin(page.url, href)
                    else:
                        part_audio = book_folder / f".pw-{volume_id}-audio.part"
                        dl_url, ext_hint, saved_part = await self._capture_playwright_download(
                            context=context,
                            page=page,
                            clickable=audio_item,
                            dest_part=part_audio,
                            response_lengths=response_lengths,
                            stage="downloading_audio",
                            progress_cb=progress_cb,
                        )
                        targets.audio_ext_hint = ext_hint or ".m4a"
                        if saved_part:
                            targets.pre_downloaded_audio = part_audio
                        elif dl_url:
                            targets.audio_url = dl_url

                # Re-open the card menu if clicking audio closed it and we still need the accompanying PDF
                if had_pdf_item and (targets.audio_url or targets.pre_downloaded_audio is not None):
                    try:
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(200)
                        if card is not None:
                            menu_opened = await self._click_card_more_menu(card, page)
                        _, pdf_item = await self._classify_export_clickables(
                            page, volume_id, menu_opened=menu_opened
                        )
                    except Exception:
                        pass

                if pdf_item is not None:
                    href = await pdf_item.get_attribute("href")
                    if href and not href.strip().lower().startswith("javascript:"):
                        targets.pdf_url = urljoin(page.url, href)
                    else:
                        part_pdf = book_folder / f".pw-{volume_id}-pdf.part"
                        pdf_dl_url, _pdf_ext, pdf_saved_part = await self._capture_playwright_download(
                            context=context,
                            page=page,
                            clickable=pdf_item,
                            dest_part=part_pdf,
                            response_lengths=response_lengths,
                            stage="downloading_pdf",
                            progress_cb=progress_cb,
                        )
                        if pdf_saved_part:
                            targets.pre_downloaded_pdf = part_pdf
                        elif pdf_dl_url:
                            targets.pdf_url = pdf_dl_url

                # Only probe /books/ab?id=<volume_id> if the card menu was not found on /books/audiobooks
                if card is None and not targets.pdf_url and targets.pre_downloaded_pdf is None:
                    try:
                        await page.goto(
                            f"{base}/books/ab?id={volume_id}",
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        await page.wait_for_timeout(500)
                        ab_audio, ab_pdf = await self._classify_export_clickables(
                            page, volume_id, menu_opened=False
                        )
                        if ab_audio is not None and not targets.audio_url and targets.pre_downloaded_audio is None:
                            href = await ab_audio.get_attribute("href")
                            if href and not href.strip().lower().startswith("javascript:"):
                                targets.audio_url = urljoin(page.url, href)
                            else:
                                part_audio = book_folder / f".pw-{volume_id}-audio.part"
                                dl_url, ext_hint, saved_part = await self._capture_playwright_download(
                                    context=context,
                                    page=page,
                                    clickable=ab_audio,
                                    dest_part=part_audio,
                                    response_lengths=response_lengths,
                                    stage="downloading_audio",
                                    progress_cb=progress_cb,
                                )
                                targets.audio_ext_hint = ext_hint or ".m4a"
                                if saved_part:
                                    targets.pre_downloaded_audio = part_audio
                                elif dl_url:
                                    targets.audio_url = dl_url
                        if ab_pdf is not None:
                            href = await ab_pdf.get_attribute("href")
                            if href and not href.strip().lower().startswith("javascript:"):
                                targets.pdf_url = urljoin(page.url, href)
                            else:
                                part_pdf = book_folder / f".pw-{volume_id}-pdf.part"
                                pdf_dl_url, _pdf_ext, pdf_saved_part = await self._capture_playwright_download(
                                    context=context,
                                    page=page,
                                    clickable=ab_pdf,
                                    dest_part=part_pdf,
                                    response_lengths=response_lengths,
                                    stage="downloading_pdf",
                                    progress_cb=progress_cb,
                                )
                                if pdf_saved_part:
                                    targets.pre_downloaded_pdf = part_pdf
                                elif pdf_dl_url:
                                    targets.pdf_url = pdf_dl_url
                    except Exception:
                        pass

                # Fallback for titles without an Export menu item: extract unencrypted audio stream from /books/listen
                if not targets.audio_url and targets.pre_downloaded_audio is None:
                    hdr = cookie_header or self.cookie_store.build_cookie_header()
                    player_url = await self._probe_listen_player_http(volume_id, hdr)
                    if not player_url:
                        player_url = await self._capture_listen_player_via_playwright(page, volume_id)
                    if player_url:
                        targets.audio_url = player_url
                        targets.audio_ext_hint = ".m4a"
                        targets.note = "Extracted directly from Google Play Books web player stream."

                if not targets.audio_url and targets.pre_downloaded_audio is None:
                    raise RuntimeError(
                        f"Neither Export menu nor web player stream available for '{book.get('title', volume_id)}' ({volume_id}). "
                        "Session cookies may need refreshing."
                    )

                return targets
            finally:
                await browser.close()

    async def _locate_book_card_in_playwright(self, page: Any, volume_id: str) -> Any | None:
        """Locate the specific card element for volume_id without matching outer <html>/<body> ancestors."""
        scoped_selector = (
            f"[data-volume-id='{volume_id}'], "
            f"gpb-volume-card:has(a[href*='id={volume_id}']), "
            f".card.audiobook:has(a[href*='id={volume_id}']), "
            f"article:has(a[href*='id={volume_id}']), "
            f"li:has(a[href*='id={volume_id}'])"
        )
        innermost_div_selector = (
            f"div:has(a[href*='id={volume_id}']):not(:has(div:has(a[href*='id={volume_id}'])))"
        )

        last_height = -1
        for _ in range(10):
            cand = page.locator(scoped_selector)
            if await cand.count() > 0:
                card = cand.first
                try:
                    await card.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                return card

            div_cand = page.locator(innermost_div_selector)
            if await div_cand.count() > 0:
                card = div_cand.first
                try:
                    await card.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                return card

            height = await page.evaluate(
                "document.documentElement ? document.documentElement.scrollHeight : 0"
            )
            if height == last_height:
                break
            last_height = height
            await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
            await page.wait_for_timeout(500)

        return None

    async def _click_card_more_menu(self, card: Any, page: Any) -> bool:
        """Click the visible 'More actions' trigger on an audiobook card, ignoring hidden menuitem buttons."""
        if card is None or await card.count() == 0:
            return False
        selector_tiers = [
            "button[aria-haspopup='menu'], [role='button'][aria-haspopup='menu'], "
            "button[aria-label*='More' i], [aria-label*='More actions' i], .more-menu-trigger",
            "button:not([role='menuitem'])",
        ]
        for selector in selector_tiers:
            candidates = card.locator(selector)
            count = await candidates.count()
            for idx in range(count - 1, -1, -1):
                btn = candidates.nth(idx)
                try:
                    if await btn.is_visible():
                        await btn.click(timeout=4000)
                        await page.wait_for_timeout(350)
                        return True
                except Exception:
                    continue
        return False

    async def _classify_export_clickables(
        self,
        page: Any,
        volume_id: str,
        *,
        menu_opened: bool = False,
    ) -> tuple[Any | None, Any | None]:
        """Inspect visible menu items and action controls to separate Audio export from PDF download."""
        overlay_selector = (
            ".cdk-overlay-container [role='menuitem']:not(.navbar-item), "
            ".cdk-overlay-container .mat-mdc-menu-item:not(.navbar-item), "
            "[role='menu'] [role='menuitem']:not(.navbar-item), "
            "[role='menu'] a:not(.navbar-item), "
            "[role='menu'] button:not(.navbar-item)"
        )
        fallback_selector = (
            "[role='menuitem']:not(.navbar-item), "
            "a[href*='download'], a[href*='export'], button:not(.navbar-item)"
        )

        candidates = page.locator(overlay_selector)
        if not menu_opened or await candidates.count() == 0:
            if menu_opened:
                # Menu was explicitly opened on a card; inspect overlay items if present, else fallback
                if await candidates.count() == 0:
                    candidates = page.locator(fallback_selector)
            else:
                candidates = page.locator(fallback_selector)

        count = await candidates.count()
        audio_item = None
        pdf_item = None

        for idx in range(min(count, 60)):
            el = candidates.nth(idx)
            try:
                if not await el.is_visible():
                    continue
                cls_attr = (await el.get_attribute("class") or "").lower()
                if "navbar-item" in cls_attr:
                    continue
                text = (await el.inner_text() or "").strip()
                aria = (await el.get_attribute("aria-label") or "").strip()
                href = (await el.get_attribute("href") or "").strip()
            except Exception:
                continue

            combined = f"{text} {aria} {href}".lower()
            if "epub" in combined or "acsm" in combined:
                continue
            if "pdf" in combined or "supplement" in combined or "companion" in combined:
                if pdf_item is None:
                    pdf_item = el
            elif any(k in combined for k in ("export", "download audio", ".m4a", "output=m4a", "audio version")):
                if audio_item is None:
                    audio_item = el

        return audio_item, pdf_item

    async def _find_export_dialog_confirm_button(self, page: Any) -> Any | None:
        """Find the confirmation button inside an Export quality dialog, preferring high quality by default."""
        dialog_roots = (
            "mat-dialog-container, "
            ".mat-mdc-dialog-container, "
            ".cdk-dialog-container, "
            "[role='dialog']"
        )
        dialogs = page.locator(dialog_roots)
        if await dialogs.count() == 0:
            return None

        prefer_standard = self.settings.export_quality == "standard"
        quality_tiers = (
            [
                "button:has-text('Export (standard)'), a:has-text('Export (standard)')",
                "button:has-text('Export (high)'), a:has-text('Export (high)')",
            ]
            if prefer_standard
            else [
                "button:has-text('Export (high)'), a:has-text('Export (high)')",
                "button:has-text('Export (standard)'), a:has-text('Export (standard)')",
            ]
        )
        quality_tiers.append(
            "button:has-text('Export'), a:has-text('Export'), "
            "button:has-text('Download'), a:has-text('Download')"
        )

        for tier in quality_tiers:
            for d_idx in range(await dialogs.count()):
                dlg = dialogs.nth(d_idx)
                try:
                    if not await dlg.is_visible():
                        continue
                except Exception:
                    continue
                btns = dlg.locator(tier)
                b_count = await btns.count()
                for b_idx in range(b_count):
                    btn = btns.nth(b_idx)
                    try:
                        if not await btn.is_visible():
                            continue
                        txt = (await btn.inner_text() or "").strip().lower()
                        if txt == "cancel":
                            continue
                        return btn
                    except Exception:
                        continue
        return None

    async def _capture_playwright_download(
        self,
        *,
        context: Any,
        page: Any,
        clickable: Any,
        dest_part: Path,
        response_lengths: dict[str, int],
        stage: str,
        progress_cb: ProgressCallback | None,
    ) -> tuple[str, str, bool]:
        loop = asyncio.get_running_loop()
        download_future: asyncio.Future[Any] = loop.create_future()

        def on_download(dl: Any) -> None:
            if not download_future.done():
                download_future.set_result(dl)

        def on_new_page(new_page: Any) -> None:
            new_page.on("download", on_download)

        page.on("download", on_download)
        context.on("page", on_new_page)
        try:
            await clickable.click()

            # Wait briefly; if an Export quality / confirmation modal opens, confirm preferred quality
            try:
                download = await asyncio.wait_for(asyncio.shield(download_future), timeout=1.8)
            except asyncio.TimeoutError:
                confirm_btn = await self._find_export_dialog_confirm_button(page)
                if confirm_btn is not None:
                    await confirm_btn.click()
                download = await asyncio.wait_for(download_future, timeout=30.0)
        finally:
            try:
                page.remove_listener("download", on_download)
                context.remove_listener("page", on_new_page)
            except Exception:
                pass

        suggested = download.suggested_filename or ""
        ext = Path(suggested).suffix.lower()
        dl_url = str(download.url or "").strip()

        # Hand signed HTTP(S) URLs directly to stream_download_to_part for live 0-100% SSE chunk progress
        if dl_url.startswith(("http://", "https://")) and self.transport is None:
            try:
                await download.cancel()
            except Exception:
                pass
            return dl_url, ext, False

        dest_part.parent.mkdir(parents=True, exist_ok=True)
        total_bytes = response_lengths.get(dl_url, 0)
        temp_path_str = await download.path()
        if temp_path_str and progress_cb:
            temp_file = Path(temp_path_str)
            if temp_file.exists():
                cur_size = temp_file.stat().st_size
                await progress_cb(cur_size, max(total_bytes, cur_size), stage)

        await download.save_as(str(dest_part))
        final_size = dest_part.stat().st_size if dest_part.exists() else 0
        if progress_cb:
            await progress_cb(final_size, max(total_bytes, final_size), stage)
        return dl_url, ext, True

    async def stream_download_to_part(
        self,
        *,
        url: str,
        part_path: Path,
        stage: str,
        progress_cb: ProgressCallback | None = None,
    ) -> tuple[int, str, str]:
        """Stream an authenticated download to a .part file while emitting live byte progress."""
        cookie_header = self._require_cookies()
        base = self.settings.base_url.rstrip("/")
        headers = {
            **self._default_headers(cookie_header),
            "Accept": "*/*",
            "Origin": base,
            "Referer": f"{base}/",
        }
        part_path.parent.mkdir(parents=True, exist_ok=True)
        if part_path.exists():
            part_path.unlink()

        downloaded = 0
        content_type = ""
        suggested_ext = ""
        last_emit_time = 0.0
        last_emit_bytes = -1

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(connect=30.0, read=300.0, write=60.0, pool=30.0),
                transport=self.transport,
            ) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if self._is_login_redirect(str(resp.url), resp.status_code):
                        raise PermissionError(
                            "Download redirected to Google Sign-In; session cookies expired."
                        )
                    if resp.status_code >= 400:
                        raise RuntimeError(f"Download failed with HTTP {resp.status_code} ({url}).")

                    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    disp = resp.headers.get("content-disposition", "")
                    fn_match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disp, re.IGNORECASE)
                    if fn_match:
                        suggested_ext = Path(fn_match.group(1).strip()).suffix.lower()

                    raw_len = resp.headers.get("content-length", "")
                    total_bytes = int(raw_len) if raw_len.isdigit() else 0

                    if progress_cb:
                        await progress_cb(0, total_bytes, stage)

                    with part_path.open("wb") as handle:
                        async for chunk in resp.aiter_bytes(chunk_size=32768):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            downloaded += len(chunk)
                            now = time.monotonic()
                            should_emit = (
                                last_emit_bytes < 0
                                or (now - last_emit_time) >= 0.08
                                or (total_bytes > 0 and (downloaded - last_emit_bytes) >= max(16384, total_bytes // 50))
                            )
                            if progress_cb and should_emit:
                                last_emit_time = now
                                last_emit_bytes = downloaded
                                await progress_cb(downloaded, total_bytes, stage)

            if progress_cb and downloaded != last_emit_bytes:
                await progress_cb(downloaded, max(total_bytes, downloaded), stage)

            return downloaded, content_type, suggested_ext
        except Exception:
            if part_path.exists():
                part_path.unlink()
            raise


def choose_audio_extension(suggested_ext: str, content_type: str) -> str:
    """Resolve a standard playable audio file extension, defaulting to .m4a."""
    if suggested_ext in STANDARD_AUDIO_EXTENSIONS:
        return suggested_ext
    ct_map = {
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "video/mp4": ".m4a",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/aac": ".aac",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/flac": ".flac",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
    }
    return ct_map.get(content_type, ".m4a")


def validate_and_finalize_audio(part_path: Path, dest_dir: Path, safe_title: str, ext: str) -> Path:
    """Validate audio container signature and atomically move .part to final audio path."""
    if not is_valid_audio_file(part_path):
        head = b""
        if part_path.exists():
            with part_path.open("rb") as handle:
                head = handle.read(65536)
            part_path.unlink()
        snippet = head[:120]
        if b"<html" in snippet.lower() or b"<!doctype" in snippet.lower():
            raise PermissionError(
                "Received an HTML page instead of an audio stream — Google session cookies likely expired."
            )
        if has_mp4_drm_encryption(head):
            raise DrmProtectedError(
                "Audio stream is encrypted with publisher DRM (CENC); DRM circumvention is not supported."
            )
        raise RuntimeError("Downloaded file is not a valid playable audio container.")

    final_ext = ext if ext in STANDARD_AUDIO_EXTENSIONS else ".m4a"
    final_path = dest_dir / f"{safe_title}{final_ext}"
    os.replace(part_path, final_path)
    return final_path


def validate_and_finalize_pdf(part_path: Path, dest_dir: Path, safe_title: str) -> tuple[Path | None, str]:
    """Validate %PDF- magic bytes and atomically move .part to <SafeTitle>.pdf, rejecting .acsm tickets."""
    if not part_path.exists():
        return None, ""
    if not is_valid_pdf_file(part_path):
        head = part_path.read_bytes()[:200]
        part_path.unlink()
        if b"adept" in head.lower() or b"fulfillmenttoken" in head.lower():
            return (
                None,
                "Audio extracted; accompanying file was an Adobe DRM (.acsm) ticket rather than a PDF.",
            )
        return None, "Audio extracted; no valid accompanying PDF returned."

    final_pdf = dest_dir / f"{safe_title}.pdf"
    os.replace(part_path, final_pdf)
    return final_pdf, ""
