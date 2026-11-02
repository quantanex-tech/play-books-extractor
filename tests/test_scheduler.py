"""Automated tests for t-006: crontab validation, schedule persistence, and scheduled sync/re-extraction."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from playbooks_extractor.app import create_app
from playbooks_extractor.config import Settings
from playbooks_extractor.scheduler import (
    CronValidationError,
    SyncScheduler,
    describe_cron_expression,
    parse_cron_expression,
)
from playbooks_extractor.storage import is_valid_audio_file, is_valid_pdf_file


def test_parse_valid_crontab_expressions_and_next_run() -> None:
    # Default daily at 2:00 AM
    daily_2am = parse_cron_expression("0 2 * * *")
    assert describe_cron_expression(daily_2am) == "Daily at 02:00"
    base = datetime(2026, 11, 2, 1, 30, 15, tzinfo=timezone.utc)
    assert daily_2am.next_run_after(base) == datetime(2026, 11, 2, 2, 0, 0, tzinfo=timezone.utc)

    # Past 2:00 AM rolls to tomorrow 2:00 AM
    after_2am = datetime(2026, 11, 2, 2, 0, 0, tzinfo=timezone.utc)
    assert daily_2am.next_run_after(after_2am) == datetime(2026, 11, 3, 2, 0, 0, tzinfo=timezone.utc)

    # Every minute
    every_min = parse_cron_expression("* * * * *")
    assert describe_cron_expression(every_min) == "Every minute"
    assert every_min.next_run_after(base) == datetime(2026, 11, 2, 1, 31, 0, tzinfo=timezone.utc)

    # Steps, lists, ranges, month/weekday names, and Sunday=7
    complex_sched = parse_cron_expression("0,30 8-18/2 * JAN-DEC MON-FRI")
    assert 0 in complex_sched.minutes and 30 in complex_sched.minutes
    assert complex_sched.hours == frozenset({8, 10, 12, 14, 16, 18})
    assert complex_sched.days_of_week == frozenset({1, 2, 3, 4, 5})

    sun_seven = parse_cron_expression("15 3 * * 7")
    assert sun_seven.days_of_week == frozenset({0})
    assert describe_cron_expression(sun_seven) == "Every Sunday at 03:15"

    # Leap-year February 29 schedule resolves next leap year (2028)
    leap = parse_cron_expression("0 4 29 2 *")
    assert leap.next_run_after(base) == datetime(2028, 2, 29, 4, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "bad_expr",
    [
        "",
        "   ",
        "0 2 *",
        "0 0 2 * * *",
        "60 2 * * *",
        "-1 2 * * *",
        "0 24 * * *",
        "0 2 0 * *",
        "0 2 32 * *",
        "0 2 * 13 *",
        "0 2 * * 8",
        "*/0 * * * *",
        "*/65 * * * *",
        "0 18-8 * * *",
        "0 2 31 2 *",  # February 31 never exists
        "0 2 31 4 *",  # April 31 never exists
        "1,,5 * * * *",
        "*/2/3 * * * *",
        "foo * * * *",
    ],
)
def test_reject_invalid_crontab_expressions(bad_expr: str) -> None:
    with pytest.raises(CronValidationError):
        parse_cron_expression(bad_expr)


@pytest.mark.asyncio
async def test_schedule_api_default_validation_and_persistence(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        use_browser="never",
    )
    app = create_app(settings)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # 1. Default schedule is enabled at 0 2 * * * (Daily at 2:00 AM)
        r_get = await client.get("/api/schedule")
        assert r_get.status_code == 200
        initial = r_get.json()
        assert initial["enabled"] is True
        assert initial["cron_expression"] == "0 2 * * *"
        assert initial["description"] == "Daily at 02:00"
        assert initial["next_run_at"] is not None

        # 2. Validate endpoint accepts valid cron & flags invalid cron
        r_val_ok = await client.post(
            "/api/schedule/validate", json={"cron_expression": "*/5 * * * *"}
        )
        assert r_val_ok.status_code == 200
        assert r_val_ok.json()["valid"] is True
        assert r_val_ok.json()["description"] == "Every 5 minutes"

        r_val_bad = await client.post(
            "/api/schedule/validate", json={"cron_expression": "99 2 * * *"}
        )
        assert r_val_bad.status_code == 200
        assert r_val_bad.json()["valid"] is False
        assert "minute" in r_val_bad.json()["error"].lower()

        # 3. PUT /api/schedule rejects invalid crontab with HTTP 400
        r_put_bad = await client.put(
            "/api/schedule", json={"enabled": True, "cron_expression": "0 2 31 2 *"}
        )
        assert r_put_bad.status_code == 400
        assert "impossible" in r_put_bad.json()["detail"].lower()

        # 4. PUT /api/schedule saves custom crontab
        r_put_ok = await client.put(
            "/api/schedule", json={"enabled": True, "cron_expression": "30 3 * * 0"}
        )
        assert r_put_ok.status_code == 200
        saved = r_put_ok.json()
        assert saved["cron_expression"] == "30 3 * * 0"
        assert saved["description"] == "Every Sunday at 03:30"

    await app.state.scheduler.stop()

    # 5. Reloading a fresh app instance preserves saved schedule from SQLite
    app_reloaded = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_reloaded), base_url="http://testserver"
    ) as client2:
        r_reloaded = await client2.get("/api/schedule")
        assert r_reloaded.status_code == 200
        assert r_reloaded.json()["cron_expression"] == "30 3 * * 0"
        assert r_reloaded.json()["enabled"] is True
    await app_reloaded.state.scheduler.stop()


@pytest.mark.asyncio
async def test_scheduled_folder_deletion_reextracts_and_next_minute_noops(tmp_path: Path) -> None:
    """
    End-to-end verification of the scheduled sync workflow:
    1. Sync & extract 2 audiobooks into /data.
    2. Delete one extracted audiobook folder from /data.
    3. Set schedule to '* * * * *' (every minute); trigger minute 1 check -> deleted book is re-extracted & saved.
    4. Trigger minute 2 check -> 0 new/missing books available -> stops immediately and performs 0 downloads.
    """
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"

    m4a_bytes = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00isomiso2mp41" + (b"\x42" * 8192)
    pdf_bytes = b"%PDF-1.7\n1 0 obj<<>>endobj\n%%EOF\n"
    audio_download_counts: dict[str, int] = {"AQAAABookOne11": 0, "AQAAABookTwo22": 0}

    library_html = """
    <!doctype html>
    <html>
      <body>
        <div aria-label="Google Account: Listener (reader@example.com)"></div>
        <gpb-volume-card>
          <div class="card audiobook">
            <a href="https://play.google.com/books/listen?id=AQAAABookOne11">Book One</a>
            <div class="title">Book One</div>
            <div class="author">Author A</div>
            <a href="/books/download/audio?id=AQAAABookOne11&output=m4a">Export</a>
            <a href="/books/download/supplement?id=AQAAABookOne11&output=pdf">Download accompanying PDF</a>
          </div>
        </gpb-volume-card>
        <gpb-volume-card>
          <div class="card audiobook">
            <a href="https://play.google.com/books/listen?id=AQAAABookTwo22">Book Two</a>
            <div class="title">Book Two</div>
            <div class="author">Author B</div>
            <a href="/books/download/audio?id=AQAAABookTwo22&output=m4a">Export</a>
          </div>
        </gpb-volume-card>
      </body>
    </html>
    """

    def mock_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        vid = request.url.params.get("id", "")
        if path in ("/books/audiobooks", "/books"):
            return httpx.Response(200, text=library_html, headers={"content-type": "text/html"})
        if path == "/books/download/audio" and vid in audio_download_counts:
            audio_download_counts[vid] += 1
            return httpx.Response(
                200,
                content=m4a_bytes,
                headers={
                    "content-type": "audio/mp4",
                    "content-length": str(len(m4a_bytes)),
                },
            )
        if path == "/books/download/supplement" and vid == "AQAAABookOne11":
            return httpx.Response(
                200,
                content=pdf_bytes,
                headers={
                    "content-type": "application/pdf",
                    "content-length": str(len(pdf_bytes)),
                },
            )
        return httpx.Response(404, text="Not found")

    settings = Settings(
        data_dir=data_dir,
        config_dir=config_dir,
        use_browser="never",
    )
    app = create_app(settings, http_transport=httpx.MockTransport(mock_handler))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        # Import cookies, sync, and extract both books initially
        r_auth = await client.post(
            "/api/auth/cookies",
            json={"raw_cookies": "SID=val1; HSID=val2; SSID=val3", "verify": True},
        )
        assert r_auth.status_code == 200

        r_sync = await client.post("/api/library/sync")
        assert r_sync.status_code == 200
        assert r_sync.json()["counts"]["total"] == 2

        r_exp = await client.post("/api/books/export-all")
        assert r_exp.status_code == 200
        assert r_exp.json()["queued_count"] == 2
        await app.state.extractor.wait_for_idle(timeout=10.0)

        folder_one = data_dir / "Book One [AQAAABookOne11]"
        folder_two = data_dir / "Book Two [AQAAABookTwo22]"
        assert is_valid_audio_file(folder_one / "Book One.m4a")
        assert is_valid_pdf_file(folder_one / "Book One.pdf")
        assert is_valid_audio_file(folder_two / "Book Two.m4a")
        assert audio_download_counts == {"AQAAABookOne11": 1, "AQAAABookTwo22": 1}

        # Set schedule to every minute ('* * * * *')
        r_sched = await client.put(
            "/api/schedule", json={"enabled": True, "cron_expression": "* * * * *"}
        )
        assert r_sched.status_code == 200
        assert r_sched.json()["description"] == "Every minute"

        # Delete Book One's extracted folder from /data
        shutil.rmtree(folder_one)
        assert not folder_one.exists()

        # Controlled clock scheduler loop test: advance clock onto a minute boundary so _loop fires automatically
        clock = [datetime(2026, 11, 2, 10, 0, 59, 920_000, tzinfo=timezone.utc)]

        def fake_now() -> datetime:
            # Advance slightly each read so loop crosses 10:01:00 within ~100ms
            clock[0] = datetime.fromtimestamp(clock[0].timestamp() + 0.04, tz=timezone.utc)
            return clock[0]

        loop_sched = SyncScheduler(
            db=app.state.db,
            cookie_store=app.state.cookie_store,
            exporter=app.state.extractor,
            events=app.state.events,
            now_fn=fake_now,
        )
        loop_sched.start()
        for _ in range(40):
            await asyncio.sleep(0.03)
            if loop_sched.get_status()["last_status"] == "exported":
                break
        await loop_sched.stop()

        # Verify Minute 1 re-extracted ONLY the deleted Book One folder and saved valid .m4a + .pdf
        status_min1 = loop_sched.get_status()
        assert status_min1["last_status"] == "exported"
        assert status_min1["last_queued_count"] == 1
        assert is_valid_audio_file(folder_one / "Book One.m4a")
        assert is_valid_pdf_file(folder_one / "Book One.pdf")
        assert audio_download_counts == {"AQAAABookOne11": 2, "AQAAABookTwo22": 1}

        # Verify Minute 2 (one minute later with all folders present) finds 0 new books and stops without downloading
        status_min2 = await loop_sched.run_scheduled_check()
        assert status_min2["last_status"] == "no_new_books"
        assert status_min2["last_queued_count"] == 0
        assert "no new or missing audiobooks" in status_min2["last_message"].lower()
        assert audio_download_counts == {"AQAAABookOne11": 2, "AQAAABookTwo22": 1}

    await app.state.scheduler.stop()
