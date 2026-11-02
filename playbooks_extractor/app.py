"""FastAPI application entrypoint providing cookie authentication, library/export APIs, and SSE."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from playbooks_extractor.config import Settings
from playbooks_extractor.cookies import (
    CookieParseError,
    CookieStore,
    parse_cookies_input,
    verify_google_play_session,
)
from playbooks_extractor.db import LibraryDatabase
from playbooks_extractor.events import EventBroker
from playbooks_extractor.scheduler import (
    CronValidationError,
    SyncScheduler,
    describe_cron_expression,
    parse_cron_expression,
    resolve_local_timezone,
)
from playbooks_extractor.storage import scan_data_directory


class CookieImportPayload(BaseModel):
    raw_cookies: str
    verify: bool = True


class ScheduleUpdatePayload(BaseModel):
    enabled: bool = True
    cron_expression: str


class ScheduleValidatePayload(BaseModel):
    cron_expression: str


def create_app(
    settings: Settings | None = None,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
    extractor_service: Any | None = None,
) -> FastAPI:
    """Create and configure the Play Books Extractor FastAPI application."""
    cfg = settings or Settings.from_env()
    cfg.ensure_dirs()

    cookie_store = CookieStore(cfg.cookies_path)
    db = LibraryDatabase(cfg.db_path)
    events = EventBroker()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.reset_interrupted_exports()
        disk_records = scan_data_directory(cfg.data_dir)
        db.reconcile_with_disk(disk_records)
        sched = getattr(app.state, "scheduler", None)
        if sched is not None:
            sched.start()
        yield
        if sched is not None:
            await sched.stop()
        svc = getattr(app.state, "extractor", None)
        if svc is not None and hasattr(svc, "shutdown"):
            await svc.shutdown()

    app = FastAPI(
        title="Play Books Extractor",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = cfg
    app.state.cookie_store = cookie_store
    app.state.db = db
    app.state.events = events
    app.state.http_transport = http_transport
    app.state.extractor = extractor_service

    # Lazily wire default ExtractorService once playbooks_client & exporter exist
    if app.state.extractor is None:
        try:
            from playbooks_extractor.exporter import ExportManager
            from playbooks_extractor.playbooks_client import PlayBooksClient

            client = PlayBooksClient(
                settings=cfg,
                cookie_store=cookie_store,
                transport=http_transport,
            )
            app.state.extractor = ExportManager(
                settings=cfg,
                db=db,
                events=events,
                client=client,
            )
        except ImportError:
            pass

    app.state.scheduler = SyncScheduler(
        db=db,
        cookie_store=cookie_store,
        exporter=app.state.extractor,
        events=events,
        default_cron=cfg.schedule_cron,
        default_enabled=cfg.schedule_enabled,
    )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "data_dir": str(cfg.data_dir),
            "config_dir": str(cfg.config_dir),
        }

    @app.get("/api/auth/status")
    async def auth_status() -> dict[str, Any]:
        summary = cookie_store.redacted_summary()
        verification = db.get_auth_state()
        if not summary["has_cookies"]:
            verification = {
                "authenticated": False,
                "checked_at": verification.get("checked_at"),
                "account_hint": "",
                "message": "No session cookies imported yet.",
            }
        return {
            **summary,
            "verification": verification,
        }

    @app.post("/api/auth/cookies")
    async def import_cookies(request: Request) -> dict[str, Any]:
        content_type = request.headers.get("content-type", "")
        raw_text = ""
        should_verify = True

        if "multipart/form-data" in content_type:
            form = await request.form()
            upload = form.get("file")
            pasted = form.get("raw_cookies")
            verify_field = form.get("verify")
            if verify_field is not None:
                should_verify = str(verify_field).lower() not in ("false", "0", "no")
            if isinstance(upload, UploadFile) or hasattr(upload, "read"):
                raw_bytes = await upload.read()
                raw_text = raw_bytes.decode("utf-8", errors="replace")
            elif isinstance(pasted, str):
                raw_text = pasted
        else:
            body = await request.json()
            payload = CookieImportPayload(**body)
            raw_text = payload.raw_cookies
            should_verify = payload.verify

        try:
            cookies = parse_cookies_input(raw_text)
        except CookieParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        cookie_store.save(cookies)

        if should_verify:
            result = await verify_google_play_session(
                cookie_store=cookie_store,
                base_url=cfg.base_url,
                user_agent=cfg.user_agent,
                transport=app.state.http_transport,
            )
            verification = db.set_auth_state(**result)
        else:
            verification = db.set_auth_state(
                authenticated=False,
                checked_at=None,
                account_hint="",
                message="Cookies saved (verification skipped).",
            )

        status_payload = {
            **cookie_store.redacted_summary(),
            "verification": verification,
        }
        await events.publish("auth_updated", status_payload)
        return status_payload

    @app.post("/api/auth/verify")
    async def verify_auth() -> dict[str, Any]:
        result = await verify_google_play_session(
            cookie_store=cookie_store,
            base_url=cfg.base_url,
            user_agent=cfg.user_agent,
            transport=app.state.http_transport,
        )
        verification = db.set_auth_state(**result)
        status_payload = {
            **cookie_store.redacted_summary(),
            "verification": verification,
        }
        await events.publish("auth_updated", status_payload)
        return status_payload

    @app.delete("/api/auth/cookies")
    async def clear_cookies() -> dict[str, Any]:
        cookie_store.clear()
        verification = db.set_auth_state(
            authenticated=False,
            checked_at=None,
            account_hint="",
            message="Session cookies cleared.",
        )
        status_payload = {
            **cookie_store.redacted_summary(),
            "verification": verification,
        }
        await events.publish("auth_updated", status_payload)
        return status_payload

    @app.get("/api/books")
    async def list_books() -> dict[str, Any]:
        disk_records = scan_data_directory(cfg.data_dir)
        books = db.reconcile_with_disk(disk_records)
        return _format_books_response(books, cfg.data_dir)

    @app.post("/api/books/reconcile")
    async def reconcile_books() -> dict[str, Any]:
        disk_records = scan_data_directory(cfg.data_dir)
        books = db.reconcile_with_disk(disk_records)
        payload = _format_books_response(books, cfg.data_dir)
        await events.publish("library_updated", payload)
        return payload

    @app.post("/api/library/sync")
    async def sync_library() -> dict[str, Any]:
        extractor = app.state.extractor
        if extractor is None:
            raise HTTPException(status_code=501, detail="Extractor engine not initialized.")
        try:
            books = await extractor.sync_library()
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return _format_books_response(books, cfg.data_dir)

    @app.post("/api/books/{volume_id}/export")
    async def export_single_book(volume_id: str) -> dict[str, Any]:
        extractor = app.state.extractor
        if extractor is None:
            raise HTTPException(status_code=501, detail="Extractor engine not initialized.")
        try:
            book = await extractor.enqueue_export(volume_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"book": book}

    @app.post("/api/books/export-all")
    async def export_all_books() -> dict[str, Any]:
        extractor = app.state.extractor
        if extractor is None:
            raise HTTPException(status_code=501, detail="Extractor engine not initialized.")
        queued = await extractor.enqueue_all_available()
        return {"queued_count": len(queued), "queued": queued}

    @app.get("/api/schedule")
    async def get_schedule() -> dict[str, Any]:
        return app.state.scheduler.get_status()

    @app.put("/api/schedule")
    async def update_schedule(payload: ScheduleUpdatePayload) -> dict[str, Any]:
        try:
            return await app.state.scheduler.update_schedule(
                enabled=payload.enabled,
                cron_expression=payload.cron_expression,
            )
        except CronValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/schedule/validate")
    async def validate_schedule(payload: ScheduleValidatePayload) -> dict[str, Any]:
        from datetime import datetime

        try:
            parsed = parse_cron_expression(payload.cron_expression)
            now = datetime.now(resolve_local_timezone())
            return {
                "valid": True,
                "cron_expression": parsed.raw_expression,
                "normalized_expression": parsed.normalized_expression,
                "description": describe_cron_expression(parsed),
                "next_run_at": parsed.next_run_after(now).isoformat(),
                "error": "",
            }
        except CronValidationError as exc:
            return {
                "valid": False,
                "cron_expression": payload.cron_expression,
                "normalized_expression": "",
                "description": "",
                "next_run_at": None,
                "error": str(exc),
            }

    @app.get("/api/books/{volume_id}/files/{kind}")
    async def download_extracted_file(volume_id: str, kind: str) -> FileResponse:
        if kind not in ("audio", "pdf"):
            raise HTTPException(status_code=400, detail="File kind must be 'audio' or 'pdf'.")
        book = db.get_book(volume_id)
        if not book or book["status"] != "extracted" or not book["folder_name"]:
            raise HTTPException(status_code=404, detail="Extracted book not found.")
        filename = book["audio_file"] if kind == "audio" else book["pdf_file"]
        if not filename:
            raise HTTPException(status_code=404, detail=f"No extracted {kind} file for this book.")
        target = (cfg.data_dir / book["folder_name"] / filename).resolve()
        if not target.is_relative_to(cfg.data_dir.resolve()) or not target.is_file():
            raise HTTPException(status_code=404, detail="File not found on disk.")
        media_type = "application/pdf" if kind == "pdf" else "audio/mp4"
        return FileResponse(target, filename=target.name, media_type=media_type)

    @app.get("/api/events")
    async def sse_events() -> StreamingResponse:
        return StreamingResponse(
            events.subscribe(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/")
        async def serve_index() -> FileResponse:
            return FileResponse(static_dir / "index.html")
    else:

        @app.get("/")
        async def root_placeholder() -> JSONResponse:
            return JSONResponse({"service": "playbooks-extractor", "status": "ok"})

    return app


def _format_books_response(books: list[dict[str, Any]], data_dir: Path) -> dict[str, Any]:
    extracted = [b for b in books if b["status"] == "extracted"]
    active = [b for b in books if b["status"] in ("exporting", "queued")]
    return {
        "data_dir": str(data_dir),
        "counts": {
            "total": len(books),
            "extracted": len(extracted),
            "exporting": len(active),
            "available": sum(1 for b in books if b["status"] == "available"),
            "failed": sum(1 for b in books if b["status"] == "failed"),
            "restricted": sum(1 for b in books if b["status"] == "restricted"),
        },
        "books": books,
    }


def main() -> None:
    cfg = Settings.from_env()
    uvicorn.run(
        "playbooks_extractor.app:create_app",
        factory=True,
        host=cfg.host,
        port=cfg.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
