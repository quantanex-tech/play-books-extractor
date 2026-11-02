"""Tests for t-003: Web UI static delivery and safe extracted audio/PDF file serving."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from playbooks_extractor.app import create_app
from playbooks_extractor.config import Settings


def _valid_m4a() -> bytes:
    return b"\x00\x00\x00\x14ftypM4A \x00\x00\x00\x00M4A " + b"\x00" * 128


def _valid_pdf() -> bytes:
    return b"%PDF-1.7\n1 0 obj<<>>endobj\n%%EOF\n"


@pytest.mark.asyncio
async def test_webui_index_assets_and_extracted_file_endpoints(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    folder = data_dir / "Dune [VolDune01]"
    folder.mkdir(parents=True)
    (folder / "Dune.m4a").write_bytes(_valid_m4a())
    (folder / "Dune.pdf").write_bytes(_valid_pdf())

    app = create_app(Settings(data_dir=data_dir, config_dir=config_dir))

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # Web UI index & assets
            idx = await client.get("/")
            assert idx.status_code == 200
            assert "Play Books Extractor" in idx.text
            assert "Google Play Books Authentication" in idx.text
            assert "Scheduled Sync &amp; Auto-Export" in idx.text
            assert "schedule-cron-input" in idx.text
            assert "Crontab Format Guide:" in idx.text
            assert "Active Exports" in idx.text

            css = await client.get("/static/styles.css")
            assert css.status_code == 200
            js = await client.get("/static/app.js")
            assert js.status_code == 200
            assert "EventSource(\"/api/events\")" in js.text

            # Reconciled extracted files downloadable safely
            audio_resp = await client.get("/api/books/VolDune01/files/audio")
            assert audio_resp.status_code == 200
            assert audio_resp.headers["content-type"].startswith("audio/mp4")
            assert audio_resp.content == _valid_m4a()

            pdf_resp = await client.get("/api/books/VolDune01/files/pdf")
            assert pdf_resp.status_code == 200
            assert pdf_resp.headers["content-type"].startswith("application/pdf")
            assert pdf_resp.content == _valid_pdf()

            bad_kind = await client.get("/api/books/VolDune01/files/other")
            assert bad_kind.status_code == 400
