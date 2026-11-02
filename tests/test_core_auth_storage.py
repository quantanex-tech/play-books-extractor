"""Tests for t-001: multi-format cookie auth, 0600 storage, session verification, and /data reconciliation."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest

from playbooks_extractor.app import create_app
from playbooks_extractor.config import Settings
from playbooks_extractor.cookies import (
    CookieParseError,
    CookieStore,
    parse_cookies_input,
    verify_google_play_session,
)
from playbooks_extractor.storage import (
    build_book_folder_name,
    sanitize_title,
    sanitize_volume_id,
    scan_data_directory,
)


def _sample_m4a_bytes(payload_size: int = 256) -> bytes:
    # Minimal valid ISO BMFF ftyp M4A box header + padding
    header = b"\x00\x00\x00\x14ftypM4A \x00\x00\x00\x00M4A "
    return header + (b"\x00" * max(0, payload_size - len(header)))


def _sample_pdf_bytes() -> bytes:
    return b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def test_parse_netscape_cookies_including_httponly() -> None:
    raw = "\n".join(
        [
            "# Netscape HTTP Cookie File",
            "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1893456000\tSID\tsecret-sid-val",
            ".google.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tsecret-sapisid",
            "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-1PSID\tsec-1psid",
            ".example.org\tTRUE\t/\tFALSE\t1893456000\tIGNORED\tnope",
        ]
    )
    cookies = parse_cookies_input(raw)
    by_name = {c.name: c for c in cookies}
    assert set(by_name) == {"SID", "SAPISID", "__Secure-1PSID"}
    assert by_name["SID"].httpOnly is True
    assert by_name["SID"].value == "secret-sid-val"
    assert by_name["__Secure-1PSID"].secure is True


def test_parse_cookie_editor_json_and_playwright_conversion() -> None:
    payload = [
        {
            "name": "SID",
            "value": "json-sid",
            "domain": ".google.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "no_restriction",
            "expirationDate": 1893456123.45,
        },
        {
            "name": "__Host-GAPS",
            "value": "host-gaps-token",
            "domain": "accounts.google.com",
            "path": "/",
            "secure": False,
            "sameSite": "unspecified",
        },
    ]
    cookies = parse_cookies_input(json.dumps(payload))
    by_name = {c.name: c for c in cookies}
    assert by_name["SID"].sameSite == "None"
    assert by_name["__Host-GAPS"].secure is True
    assert by_name["__Host-GAPS"].sameSite is None

    pw_host = by_name["__Host-GAPS"].to_playwright()
    assert pw_host["url"] == "https://accounts.google.com"
    assert "domain" not in pw_host
    assert pw_host["secure"] is True


def test_parse_curl_and_raw_cookie_header() -> None:
    curl_cmd = (
        "curl 'https://play.google.com/books/audiobooks' \\\n"
        "  -H 'accept: text/html' \\\n"
        "  -H 'cookie: SID=curl-sid; HSID=curl-hsid; SSID=curl-ssid'"
    )
    parsed_curl = parse_cookies_input(curl_cmd)
    assert {c.name: c.value for c in parsed_curl} == {
        "SID": "curl-sid",
        "HSID": "curl-hsid",
        "SSID": "curl-ssid",
    }

    raw_hdr = "Cookie: SID=hdr-sid; SAPISID=hdr-sapisid"
    parsed_hdr = parse_cookies_input(raw_hdr)
    assert {c.name: c.value for c in parsed_hdr} == {
        "SID": "hdr-sid",
        "SAPISID": "hdr-sapisid",
    }

    with pytest.raises(CookieParseError):
        parse_cookies_input("   ")


def test_cookie_store_permissions_and_redaction(tmp_path: Path) -> None:
    cookies_file = tmp_path / "config" / "cookies.json"
    store = CookieStore(cookies_file)
    cookies = parse_cookies_input("SID=top-secret-value; HSID=another-secret; SSID=third-secret")
    store.save(cookies)

    mode = stat.S_IMODE(cookies_file.stat().st_mode)
    assert mode == 0o600

    summary = store.redacted_summary()
    assert summary["has_cookies"] is True
    assert summary["cookie_count"] == 3
    assert summary["has_essential_google_cookies"] is True
    serialized_summary = json.dumps(summary)
    assert "top-secret-value" not in serialized_summary
    assert "another-secret" not in serialized_summary


def test_title_and_volume_id_sanitization() -> None:
    assert sanitize_title('Project Hail Mary: A Novel / Unabridged? *Special*') == (
        "Project Hail Mary - A Novel - Unabridged - Special"
    )
    assert sanitize_volume_id("../abc_123-XYZ/../../etc") == "abc_123-XYZetc"
    assert build_book_folder_name("Dune: Messiah", "vol_001") == "Dune - Messiah [vol_001]"


@pytest.mark.asyncio
async def test_auth_api_and_disk_reconciliation(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    data_dir.mkdir()
    config_dir.mkdir()

    # Pre-populate one extracted audiobook folder with .m4a + .pdf + ignored .part
    book_folder = data_dir / "Project Hail Mary [vol_phm_01]"
    book_folder.mkdir()
    (book_folder / "Project Hail Mary.m4a").write_bytes(_sample_m4a_bytes(512))
    (book_folder / "Project Hail Mary.pdf").write_bytes(_sample_pdf_bytes())
    (book_folder / "incomplete.m4a.part").write_bytes(_sample_m4a_bytes(1024))

    # Pre-populate a folder with an invalid .acsm masquerading as .pdf alongside valid .m4a
    book2_folder = data_dir / "The Hobbit [vol_hobbit_02]"
    book2_folder.mkdir()
    (book2_folder / "The Hobbit.m4a").write_bytes(_sample_m4a_bytes(300))
    (book2_folder / "The Hobbit.pdf").write_bytes(b"<fulfillmentToken xmlns='http://ns.adobe.com/adept'/>")

    scanned = scan_data_directory(data_dir)
    assert set(scanned) == {"vol_phm_01", "vol_hobbit_02"}
    assert scanned["vol_phm_01"].pdf_file == "Project Hail Mary.pdf"
    assert scanned["vol_hobbit_02"].pdf_file == ""  # .acsm rejected cleanly!

    # Mock upstream Google Play Books endpoint
    auth_mode = {"valid": True}

    async def mock_google_handler(request: httpx.Request) -> httpx.Response:
        cookie_hdr = request.headers.get("cookie", "")
        if auth_mode["valid"] and "SID=valid-sid" in cookie_hdr:
            return httpx.Response(
                200,
                text="<html><body><div>reader@gmail.com</div><div data-Authenticated='1'>My Books</div></body></html>",
            )
        return httpx.Response(
            302,
            headers={"Location": "https://accounts.google.com/ServiceLogin?continue=https://play.google.com/books"},
        )

    async def mock_login_page(request: httpx.Request) -> httpx.Response:
        if "accounts.google.com" in str(request.url):
            return httpx.Response(200, text="<html>Sign in to Google</html>")
        return await mock_google_handler(request)

    transport = httpx.MockTransport(mock_login_page)
    settings = Settings(
        data_dir=data_dir,
        config_dir=config_dir,
        base_url="https://play.google.com",
    )
    app = create_app(settings, http_transport=transport)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # 1. Reconciled books on startup
            books_resp = await client.get("/api/books")
            assert books_resp.status_code == 200
            books_data = books_resp.json()
            assert books_data["counts"]["extracted"] == 2
            by_id = {b["volume_id"]: b for b in books_data["books"]}
            assert by_id["vol_phm_01"]["status"] == "extracted"
            assert by_id["vol_phm_01"]["has_pdf"] is True
            assert by_id["vol_phm_01"]["audio_size_bytes"] == 512
            assert by_id["vol_hobbit_02"]["has_pdf"] is False

            # 2. Import valid cookies via multipart file upload
            cookies_txt = (
                "# Netscape HTTP Cookie File\n"
                "#HttpOnly_.google.com\tTRUE\t/\tTRUE\t1893456000\tSID\tvalid-sid\n"
                ".google.com\tTRUE\t/\tTRUE\t1893456000\tHSID\tvalid-hsid\n"
            )
            upload_resp = await client.post(
                "/api/auth/cookies",
                files={"file": ("cookies.txt", cookies_txt.encode("utf-8"), "text/plain")},
            )
            assert upload_resp.status_code == 200
            auth_payload = upload_resp.json()
            assert auth_payload["has_cookies"] is True
            assert auth_payload["verification"]["authenticated"] is True
            assert auth_payload["verification"]["account_hint"] == "reader@gmail.com"
            assert "valid-sid" not in json.dumps(auth_payload)

            # 3. Simulate session expiry -> redirect to accounts.google.com
            auth_mode["valid"] = False
            verify_resp = await client.post("/api/auth/verify")
            assert verify_resp.status_code == 200
            assert verify_resp.json()["verification"]["authenticated"] is False

            # 4. Delete one folder on disk and verify reconciliation resets it
            for child in book2_folder.iterdir():
                child.unlink()
            book2_folder.rmdir()

            rec_resp = await client.post("/api/books/reconcile")
            assert rec_resp.status_code == 200
            rec_by_id = {b["volume_id"]: b for b in rec_resp.json()["books"]}
            assert rec_by_id["vol_phm_01"]["status"] == "extracted"
            assert rec_by_id["vol_hobbit_02"]["status"] == "available"


def test_settings_default_export_quality_is_high(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PLAYBOOKS_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("PLAYBOOKS_CONFIG_DIR", str(tmp_path / "c"))
    monkeypatch.delenv("PLAYBOOKS_EXPORT_QUALITY", raising=False)
    assert Settings.from_env().export_quality == "high"

    monkeypatch.setenv("PLAYBOOKS_EXPORT_QUALITY", "standard")
    assert Settings.from_env().export_quality == "standard"


@pytest.mark.asyncio
async def test_verify_session_ignores_google_fonts_wght_token(tmp_path: Path) -> None:
    store = CookieStore(tmp_path / "cookies.json")
    store.save(parse_cookies_input("SID=s1; HSID=h1; SSID=ss1"))

    font_only_html = (
        "<!doctype html><html><head>"
        "<link href='https://fonts.googleapis.com/css2?family=Google+Sans:wght@6..144&display=swap' rel='stylesheet'>"
        "<img src='/icons/logo@2x.png'>"
        "</head><body><a href='https://play.google.com/books/audiobooks'>My Books</a>"
        "<span>Sign out</span></body></html>"
    )
    account_html = (
        "<!doctype html><html><head>"
        "<link href='https://fonts.googleapis.com/css2?family=Google+Sans:wght@6..144' rel='stylesheet'>"
        "</head><body>"
        "<a aria-label='Google Account: Test Listener  \n(listener.test@gmail.com), Google membership' "
        "href='https://accounts.google.com/SignOutOptions'></a>"
        "</body></html>"
    )
    state = {"html": font_only_html}

    async def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=state["html"])

    res_no_email = await verify_google_play_session(
        cookie_store=store,
        base_url="https://play.google.com",
        user_agent="TestUA",
        transport=httpx.MockTransport(handler),
    )
    assert res_no_email["authenticated"] is True
    assert res_no_email["account_hint"] == ""

    state["html"] = account_html
    res_with_email = await verify_google_play_session(
        cookie_store=store,
        base_url="https://play.google.com",
        user_agent="TestUA",
        transport=httpx.MockTransport(handler),
    )
    assert res_with_email["authenticated"] is True
    assert res_with_email["account_hint"] == "listener.test@gmail.com"

