"""Integration tests for t-002: library discovery, .m4a + .pdf extraction, and live SSE progress."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest

from playbooks_extractor.app import create_app
from playbooks_extractor.config import Settings
from playbooks_extractor.cookies import CookieStore
from playbooks_extractor.playbooks_client import PlayBooksClient
from playbooks_extractor.storage import is_valid_audio_file, is_valid_pdf_file


def _chunked_m4a_payload(total_chunks: int = 8, chunk_bytes: int = 40960) -> list[bytes]:
    header = b"\x00\x00\x00\x14ftypM4A \x00\x00\x00\x00M4A "
    first = header + (b"\x11" * (chunk_bytes - len(header)))
    rest = [bytes([i % 251]) * chunk_bytes for i in range(1, total_chunks)]
    return [first, *rest]


def _chunked_pdf_payload() -> list[bytes]:
    body = (
        b"%PDF-1.7\n"
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
        + (b"% Accompanying audiobook reference map and diagrams\n" * 400)
        + b"%%EOF\n"
    )
    mid = len(body) // 2
    return [body[:mid], body[mid:]]


class AsyncChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], delay_sec: float = 0.015) -> None:
        self._chunks = chunks
        self._delay_sec = delay_sec

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            if self._delay_sec > 0:
                await asyncio.sleep(self._delay_sec)
            yield chunk


@pytest.mark.asyncio
async def test_discovery_extraction_pdf_and_live_sse_progress(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"

    m4a_chunks = _chunked_m4a_payload(total_chunks=8, chunk_bytes=40960)
    m4a_total_len = sum(len(c) for c in m4a_chunks)
    pdf_chunks = _chunked_pdf_payload()
    pdf_total_len = sum(len(c) for c in pdf_chunks)

    library_html = """
    <!doctype html>
    <html>
      <body>
        <div data-Authenticated="1">listener@gmail.com</div>
        <div class="library-grid">
          <article data-volume-id="AbCdEf123456">
            <h3 class="book-title">Children of Time: Book 1</h3>
            <div class="book-author">Adrian Tchaikovsky</div>
            <img src="https://play.google.com/books/content?id=AbCdEf123456&img=1" />
            <a href="/books/download/audio?id=AbCdEf123456&output=m4a">Export</a>
            <a href="/books/download/supplement?id=AbCdEf123456&output=pdf">Download PDF</a>
          </article>

          <article>
            <a href="/books/ab?id=XyZ987654321" aria-label="Audiobook XyZ987654321"></a>
          </article>

          <article data-volume-id="LockedVol999" data-export-supported="false">
            <h3 class="book-title">Locked Publisher Audiobook</h3>
            <div class="book-author">Restricted Press</div>
          </article>
        </div>

        <script>
          AF_initDataCallback({
            key: 'ds:1',
            hash: '2',
            data: [
              [
                ["AfInitVol777", "The Martian", "Andy Weir", "https://books.googleusercontent.com/cover777.jpg", "/books/download/audio?id=AfInitVol777&output=m4a"]
              ]
            ],
            sideChannel: {}
          });
        </script>
      </body>
    </html>
    """

    detail_xyz_html = """
    <html>
      <body>
        <h1>Exhalation: Stories</h1>
        <a href="/books/download/audio?id=XyZ987654321&output=m4a">Export audio</a>
        <a href="/books/download/acsm?id=XyZ987654321&output=pdf">Download PDF</a>
      </body>
    </html>
    """

    async def mock_upstream(request: httpx.Request) -> httpx.Response:
        cookie_hdr = request.headers.get("cookie", "")
        if "SID=active-session" not in cookie_hdr:
            return httpx.Response(
                302,
                headers={"Location": "https://accounts.google.com/ServiceLogin"},
            )

        path = request.url.path
        qs = str(request.url.query)

        if path in ("/books", "/books/audiobooks"):
            return httpx.Response(200, text=library_html)

        if path == "/books/v1/volumes/XyZ987654321":
            return httpx.Response(
                200,
                json={
                    "volumeInfo": {
                        "title": "Exhalation: Stories",
                        "authors": ["Ted Chiang"],
                        "imageLinks": {"thumbnail": "https://books.google.com/exhalation.jpg"},
                    }
                },
            )

        if path == "/books/ab" and "id=XyZ987654321" in qs:
            return httpx.Response(200, text=detail_xyz_html)

        if path == "/books/listen" and "id=LockedVol999" in qs:
            listen_html = (
                "<html><body><script>"
                "AF_initDataCallback({key: 'ds:2', data: ["
                '"https:\\/\\/play.google.com\\/videoplayback?expire\\\\u003d1793604614\\\\u0026amp;itag\\\\u003d139\\\\u0026amp;clen\\\\u003d120000\\\\u0026amp;source\\\\u003dplay_audiobooks_v2",'
                '"https:\\/\\/play.google.com\\/videoplayback?expire\\\\u003d1793604614\\\\u0026amp;itag\\\\u003d140\\\\u0026amp;clen\\\\u003d'
                + str(m4a_total_len)
                + '\\\\u0026amp;alr\\\\u003dyes\\\\u0026amp;range\\\\u003d656-9999\\\\u0026amp;source\\\\u003dplay_audiobooks_v2"'
                "]});</script></body></html>"
            )
            return httpx.Response(200, text=listen_html)

        if path == "/videoplayback":
            # Confirm itag=140 (higher quality) was chosen and range/alr params were stripped
            assert "itag=140" in qs
            assert "range=" not in qs
            assert "alr=" not in qs
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "audio/mp4",
                    "Content-Length": str(m4a_total_len),
                },
                stream=AsyncChunkStream(m4a_chunks, delay_sec=0.01),
            )

        if path == "/books/download/audio":
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "audio/mp4",
                    "Content-Length": str(m4a_total_len),
                    "Content-Disposition": 'attachment; filename="audiobook.m4a"',
                },
                stream=AsyncChunkStream(m4a_chunks, delay_sec=0.02),
            )

        if path == "/books/download/supplement":
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Length": str(pdf_total_len),
                    "Content-Disposition": 'attachment; filename="supplement.pdf"',
                },
                stream=AsyncChunkStream(pdf_chunks, delay_sec=0.015),
            )

        if path == "/books/download/acsm":
            acsm_xml = b'<?xml version="1.0"?><fulfillmentToken xmlns="http://ns.adobe.com/adept"/>'
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "application/vnd.adobe.adept+xml",
                    "Content-Length": str(len(acsm_xml)),
                },
                content=acsm_xml,
            )

        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(mock_upstream)
    settings = Settings(
        data_dir=data_dir,
        config_dir=config_dir,
        base_url="https://play.google.com",
        books_api_url="https://play.google.com/books/v1/volumes",
        use_browser="never",
    )
    app = create_app(settings, http_transport=transport)

    async with app.router.lifespan_context(app):
        # Subscribe directly to the SSE broker in parallel to capture every live progress frame
        captured_progress: list[dict] = []
        stop_sse = asyncio.Event()

        async def collect_sse() -> None:
            async for raw_frame in app.state.events.subscribe(heartbeat_seconds=0.5):
                if stop_sse.is_set():
                    break
                if raw_frame.startswith("event: book_progress"):
                    for line in raw_frame.splitlines():
                        if line.startswith("data: "):
                            captured_progress.append(json.loads(line[6:]))

        sse_task = asyncio.create_task(collect_sse())
        await asyncio.sleep(0)  # Allow SSE subscriber task to register before firing requests

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                # 1. Import active cookies
                auth_resp = await client.post(
                    "/api/auth/cookies",
                    json={"raw_cookies": "SID=active-session; HSID=h1; SSID=s1"},
                )
                assert auth_resp.status_code == 200
                assert auth_resp.json()["verification"]["authenticated"] is True

                # 2. Sync Google Play Books library (DOM + AF_initDataCallback + Volume API enrichment)
                sync_resp = await client.post("/api/library/sync")
                assert sync_resp.status_code == 200
                books = {b["volume_id"]: b for b in sync_resp.json()["books"]}
                assert set(books) == {"AbCdEf123456", "XyZ987654321", "AfInitVol777", "LockedVol999"}
                assert books["AbCdEf123456"]["title"] == "Children of Time: Book 1"
                assert books["AbCdEf123456"]["has_pdf"] is True
                assert books["XyZ987654321"]["title"] == "Exhalation: Stories"
                assert books["XyZ987654321"]["authors"] == "Ted Chiang"
                assert books["AfInitVol777"]["title"] == "The Martian"
                # Titles lacking a UI Export button remain exportable via the /books/listen player fallback
                assert books["LockedVol999"]["status"] == "available"

                # 3. Trigger export of Children of Time (has both .m4a and accompanying .pdf)
                exp_resp = await client.post("/api/books/AbCdEf123456/export")
                assert exp_resp.status_code == 200

                # Wait for background export worker to finish
                await app.state.extractor.wait_for_idle(timeout=15.0)
                await asyncio.sleep(0.05)

                # Inspect live progress sequence for AbCdEf123456
                cot_events = [e for e in captured_progress if e["volume_id"] == "AbCdEf123456"]
                percents = [e["progress_percent"] for e in cot_events]
                assert len(percents) >= 4
                assert percents[0] == 0.0
                assert percents[-1] == 100.0
                # Progress percentages must increase monotonically from 0.0 to 100.0
                assert percents == sorted(percents)
                assert any(5.0 < p < 89.0 for p in percents), f"Expected intermediate audio %, got {percents}"
                assert any(89.5 <= p < 100.0 for p in percents), f"Expected PDF stage %, got {percents}"

                # Verify folder & files on disk in /data/<Sanitized Title> [<VolumeID>]/
                expected_folder = data_dir / "Children of Time - Book 1 [AbCdEf123456]"
                assert expected_folder.is_dir()
                m4a_file = expected_folder / "Children of Time - Book 1.m4a"
                pdf_file = expected_folder / "Children of Time - Book 1.pdf"
                assert is_valid_audio_file(m4a_file)
                assert m4a_file.stat().st_size == m4a_total_len
                assert is_valid_pdf_file(pdf_file)
                assert pdf_file.stat().st_size == pdf_total_len
                assert not list(expected_folder.glob("*.part"))

                # 4. Trigger export-all for remaining books (XyZ987654321 + AfInitVol777 + LockedVol999 via /books/listen)
                batch_resp = await client.post("/api/books/export-all")
                assert batch_resp.status_code == 200
                assert batch_resp.json()["queued_count"] == 3

                await app.state.extractor.wait_for_idle(timeout=15.0)

                final_books = {
                    b["volume_id"]: b for b in (await client.get("/api/books")).json()["books"]
                }
                assert final_books["XyZ987654321"]["status"] == "extracted"
                # XyZ987654321 returned an .acsm ticket for PDF: audio extracted, .acsm NOT saved as .pdf!
                exh_folder = data_dir / "Exhalation - Stories [XyZ987654321]"
                assert is_valid_audio_file(exh_folder / "Exhalation - Stories.m4a")
                assert not (exh_folder / "Exhalation - Stories.pdf").exists()
                assert "Adobe DRM (.acsm)" in final_books["XyZ987654321"]["error_message"]

                assert final_books["AfInitVol777"]["status"] == "extracted"
                martian_folder = data_dir / "The Martian [AfInitVol777]"
                assert is_valid_audio_file(martian_folder / "The Martian.m4a")

                assert final_books["LockedVol999"]["status"] == "extracted"
                player_folder = data_dir / "Locked Publisher Audiobook [LockedVol999]"
                assert is_valid_audio_file(player_folder / "Locked Publisher Audiobook.m4a")
                assert "web player stream" in final_books["LockedVol999"]["error_message"]
        finally:
            stop_sse.set()
            sse_task.cancel()
            await asyncio.gather(sse_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_expired_session_html_response_rejected_cleanly(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"

    async def mock_expired_download(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/books", "/books/audiobooks"):
            return httpx.Response(
                200,
                text=(
                    "<html><body><div data-Authenticated='1'>u@gmail.com</div>"
                    "<article data-volume-id='BadAudio123'>"
                    "<h3 class='book-title'>Expired Session Book</h3>"
                    "<a href='/books/download/audio?id=BadAudio123&output=m4a'>Export</a>"
                    "</article></body></html>"
                ),
            )
        # Simulate Google returning a 200 HTML login challenge instead of audio bytes
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<!doctype html><html><body>Sign in to continue</body></html>",
        )

    settings = Settings(
        data_dir=data_dir,
        config_dir=config_dir,
        base_url="https://play.google.com",
        use_browser="never",
    )
    app = create_app(settings, http_transport=httpx.MockTransport(mock_expired_download))

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            await client.post("/api/auth/cookies", json={"raw_cookies": "SID=s; HSID=h"})
            await client.post("/api/library/sync")
            await client.post("/api/books/BadAudio123/export")
            await app.state.extractor.wait_for_idle(timeout=10.0)

            books = (await client.get("/api/books")).json()["books"]
            assert len(books) == 1
            assert books[0]["status"] == "failed"
            assert "HTML page instead of an audio stream" in books[0]["error_message"]
            assert not list(data_dir.rglob("*.part"))
            assert not list(data_dir.rglob("*.m4a"))


@pytest.mark.asyncio
async def test_angular_gpb_volume_card_ignores_author_links_and_prunes_stale_rows(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"

    angular_spa_html = """
    <!doctype html>
    <html lang="en-GB">
      <body>
        <div class="scrollarea">
          <a role="menuitem" class="navbar-item" href="/books">Books 87</a>
          <a role="menuitem" class="navbar-item" href="/books/wishlist">Wishlist 12</a>
        </div>
        <gpb-volume-card>
          <div class="card audiobook">
            <a class="card-link" title="Silly Stories About Vegetables, Books 1-4"
               href="https://play.google.com/books/listen?id=AQAAAEBM0iSSeM"></a>
            <div class="cover-image-container">
              <a aria-label="Silly Stories About Vegetables, Books 1-4"
                 href="https://play.google.com/books/listen?id=AQAAAEBM0iSSeM">
                <img src="https://books.google.com/books/content?id=AQAAAEBM0iSSeM&img=1" />
              </a>
            </div>
            <div class="metadata">
              <a href="https://play.google.com/books/listen?id=AQAAAEBM0iSSeM">Silly Stories About Vegetables, Books 1-4</a>
              <a href="https://play.google.com/store/books/author?id=Paul+Cook">Paul Cook</a>
            </div>
          </div>
        </gpb-volume-card>
        <gpb-volume-card>
          <div class="card audiobook">
            <a class="card-link" title="The Republic"
               href="https://play.google.com/books/listen?id=AQAAAPlatoVol1"></a>
            <div class="metadata">
              <a href="https://play.google.com/books/listen?id=AQAAAPlatoVol1">The Republic</a>
              <a href="https://play.google.com/store/books/author?id=Plato">Plato</a>
            </div>
          </div>
        </gpb-volume-card>
      </body>
    </html>
    """

    async def mock_spa(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/books", "/books/audiobooks"):
            return httpx.Response(200, text=angular_spa_html)
        return httpx.Response(404)

    settings = Settings(
        data_dir=data_dir,
        config_dir=config_dir,
        base_url="https://play.google.com",
        use_browser="never",
    )
    app = create_app(settings, http_transport=httpx.MockTransport(mock_spa))

    async with app.router.lifespan_context(app):
        # Seed stale bogus author-first-name rows like those produced before t-004
        app.state.db.upsert_discovered_books(
            [
                {"volume_id": "Paul", "title": "Silly Stories About Vegetables, Books 1-4", "authors": "Paul Cook"},
                {"volume_id": "Timothy", "title": "Solving the Procrastination Puzzle", "authors": "Timothy A. Pychyl"},
            ]
        )
        assert {b["volume_id"] for b in app.state.db.list_books()} == {"Paul", "Timothy"}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            await client.post("/api/auth/cookies", json={"raw_cookies": "SID=s1; HSID=h1; SSID=s2"})
            sync_resp = await client.post("/api/library/sync")
            assert sync_resp.status_code == 200
            books = {b["volume_id"]: b for b in sync_resp.json()["books"]}

            assert set(books) == {"AQAAAEBM0iSSeM", "AQAAAPlatoVol1"}
            assert "Paul" not in books
            assert "Timothy" not in books
            assert "Plato" not in books
            assert books["AQAAAEBM0iSSeM"]["title"] == "Silly Stories About Vegetables, Books 1-4"
            assert books["AQAAAEBM0iSSeM"]["authors"] == "Paul Cook"
            assert "AQAAAEBM0iSSeM" in books["AQAAAEBM0iSSeM"]["cover_url"]
            assert books["AQAAAPlatoVol1"]["authors"] == "Plato"


@pytest.mark.asyncio
async def test_export_dialog_selects_high_quality_without_role_dialog_and_ignores_navbar_items(
    tmp_path: Path,
) -> None:
    """Verify <mat-dialog-container> (no role='dialog') defaults to Export (high) and navbar menuitems are skipped."""

    class FakeElement:
        def __init__(self, text: str, cls: str = "", aria: str = "", href: str = "", visible: bool = True) -> None:
            self._text = text
            self._cls = cls
            self._aria = aria
            self._href = href
            self._visible = visible

        async def is_visible(self) -> bool:
            return self._visible

        async def inner_text(self) -> str:
            return self._text

        async def get_attribute(self, name: str) -> str | None:
            return {"class": self._cls, "aria-label": self._aria, "href": self._href}.get(name)

    class FakeLocator:
        def __init__(self, items: list[Any]) -> None:
            self._items = items

        async def count(self) -> int:
            return len(self._items)

        def nth(self, idx: int) -> Any:
            return self._items[idx]

    class FakeDialog(FakeElement):
        def __init__(self, buttons: list[FakeElement]) -> None:
            super().__init__("Export quality", cls="mat-mdc-dialog-container")
            self._buttons = buttons

        def locator(self, selector: str) -> FakeLocator:
            if "Export (high)" in selector:
                return FakeLocator([b for b in self._buttons if "Export (high)" in b._text])
            if "Export (standard)" in selector:
                return FakeLocator([b for b in self._buttons if "Export (standard)" in b._text])
            return FakeLocator([b for b in self._buttons if "Export" in b._text or "Download" in b._text])

    cancel_btn = FakeElement("Cancel")
    std_btn = FakeElement("Export (standard)")
    high_btn = FakeElement("Export (high)")
    dialog = FakeDialog([cancel_btn, std_btn, high_btn])

    navbar_item = FakeElement("Books\n87", cls="mat-mdc-menu-item navbar-item", href="/books")
    export_item = FakeElement("Export", cls="mat-mdc-menu-item")
    pdf_item = FakeElement("Download accompanying PDF", cls="mat-mdc-menu-item")

    class FakePage:
        def locator(self, selector: str) -> FakeLocator:
            if "mat-dialog-container" in selector:
                return FakeLocator([dialog])
            if ".cdk-overlay-container" in selector:
                return FakeLocator([export_item, pdf_item])
            return FakeLocator([navbar_item, export_item, pdf_item])

    page = FakePage()
    high_client = PlayBooksClient(
        Settings(data_dir=tmp_path / "d", config_dir=tmp_path / "c", export_quality="high"),
        CookieStore(tmp_path / "c" / "cookies.json"),
    )
    chosen_high = await high_client._find_export_dialog_confirm_button(page)
    assert chosen_high is high_btn

    std_client = PlayBooksClient(
        Settings(data_dir=tmp_path / "d", config_dir=tmp_path / "c", export_quality="standard"),
        CookieStore(tmp_path / "c" / "cookies.json"),
    )
    chosen_std = await std_client._find_export_dialog_confirm_button(page)
    assert chosen_std is std_btn

    audio_el, pdf_el = await high_client._classify_export_clickables(page, "AQAAAECimSvZmM", menu_opened=True)
    assert audio_el is export_item
    assert pdf_el is pdf_item


@pytest.mark.asyncio
async def test_cenc_drm_encrypted_stream_rejected_cleanly(tmp_path: Path) -> None:
    """Verify genuinely CENC/DRM-encrypted ISO BMFF streams are rejected as restricted without leaving empty folders."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"

    cenc_mp4_bytes = (
        b"\x00\x00\x00\x18ftypdash\x00\x00\x00\x00iso6mp41"
        b"\x00\x00\x00\x40moov\x00\x00\x00\x38stsdenca\x00\x00\x00\x18sinfschmcencpssh"
        + (b"\x00" * 512)
    )

    async def mock_cenc(request: httpx.Request) -> httpx.Response:
        if request.url.path in ("/books", "/books/audiobooks"):
            return httpx.Response(
                200,
                text=(
                    "<html><body><span>Sign out</span>"
                    "<gpb-volume-card><div class='card audiobook'>"
                    "<a class='card-link' title='Encrypted Title' href='https://play.google.com/books/listen?id=AQAAACencDrm01'></a>"
                    "</div></gpb-volume-card></body></html>"
                ),
            )
        if request.url.path == "/books/listen":
            return httpx.Response(
                200,
                text='<script>var u="https:\\/\\/play.google.com\\/videoplayback?itag\\u003d140\\u0026amp;clen\\u003d600";</script>',
            )
        if request.url.path == "/videoplayback":
            return httpx.Response(200, headers={"Content-Type": "audio/mp4"}, content=cenc_mp4_bytes)
        return httpx.Response(404)

    settings = Settings(
        data_dir=data_dir,
        config_dir=config_dir,
        base_url="https://play.google.com",
        use_browser="never",
    )
    app = create_app(settings, http_transport=httpx.MockTransport(mock_cenc))

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            await client.post("/api/auth/cookies", json={"raw_cookies": "SID=s1; HSID=h1"})
            await client.post("/api/library/sync")
            await client.post("/api/books/AQAAACencDrm01/export")
            await app.state.extractor.wait_for_idle(timeout=10.0)

            books = (await client.get("/api/books")).json()["books"]
            assert len(books) == 1
            assert books[0]["status"] == "restricted"
            assert "DRM (CENC)" in books[0]["error_message"]
            assert not list(data_dir.iterdir())



