"""Async background export queue orchestrating audiobook + PDF extraction and SSE live progress."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from playbooks_extractor.config import Settings
from playbooks_extractor.db import LibraryDatabase
from playbooks_extractor.events import EventBroker
from playbooks_extractor.playbooks_client import (
    DrmProtectedError,
    PlayBooksClient,
    choose_audio_extension,
    validate_and_finalize_audio,
    validate_and_finalize_pdf,
)
from playbooks_extractor.storage import (
    resolve_book_folder,
    sanitize_title,
    scan_data_directory,
)


class ExportManager:
    """Manages library discovery, background export workers, and live 0-100% SSE progress."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: LibraryDatabase,
        events: EventBroker,
        client: PlayBooksClient,
        concurrency: int = 2,
    ) -> None:
        self.settings = settings
        self.db = db
        self.events = events
        self.client = client
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._active_tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def active_count(self) -> int:
        return sum(1 for task in self._active_tasks.values() if not task.done())

    async def shutdown(self) -> None:
        tasks = list(self._active_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.clear()

    async def wait_for_idle(self, timeout: float | None = 30.0) -> None:
        """Await completion of all currently queued/running export tasks (useful in tests/scheduler)."""
        tasks = list(self._active_tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)

    async def sync_library(self) -> list[dict[str, Any]]:
        """Discover audiobooks from Google Play Books and reconcile with /data on disk."""
        discovered = await self.client.discover_audiobooks()
        self.db.upsert_discovered_books(discovered)
        disk_records = scan_data_directory(self.settings.data_dir)
        if discovered:
            keep_ids = (
                {str(b["volume_id"]).strip() for b in discovered}
                | set(disk_records.keys())
                | set(self._active_tasks.keys())
            )
            self.db.prune_stale_unextracted(keep_ids)
        books = self.db.reconcile_with_disk(disk_records)
        await self.events.publish("library_synced", {"count": len(books), "books": books})
        return books

    async def enqueue_export(self, volume_id: str) -> dict[str, Any]:
        """Queue a single audiobook for asynchronous extraction if not already active."""
        book = self.db.get_book(volume_id)
        if book is None:
            raise KeyError(f"Audiobook '{volume_id}' not found in library. Sync library first.")
        if not book["export_supported"]:
            raise ValueError(
                f"Publisher does not permit exporting '{book['title']}' ({volume_id})."
            )
        if volume_id in self._active_tasks and not self._active_tasks[volume_id].done():
            return book

        updated = self.db.update_export_progress(
            volume_id,
            status="queued",
            progress_percent=0.0,
            stage="queued",
            downloaded_bytes=0,
            total_bytes=0,
            error_message="",
        )
        assert updated is not None
        await self.events.publish("book_progress", updated)

        task = asyncio.create_task(self._run_export_job(volume_id), name=f"export-{volume_id}")
        self._active_tasks[volume_id] = task
        task.add_done_callback(lambda _t, vid=volume_id: self._active_tasks.pop(vid, None))
        return updated

    async def enqueue_all_available(self) -> list[dict[str, Any]]:
        """Queue all exportable audiobooks that are not yet extracted or active."""
        books = self.db.list_books()
        queued: list[dict[str, Any]] = []
        for book in books:
            if book["export_supported"] and book["status"] in ("available", "failed"):
                queued.append(await self.enqueue_export(book["volume_id"]))
        return queued

    async def _run_export_job(self, volume_id: str) -> None:
        async with self._semaphore:
            book = self.db.get_book(volume_id)
            if book is None:
                return

            safe_title = sanitize_title(book["title"])
            book_folder = resolve_book_folder(self.settings.data_dir, book["title"], volume_id)

            try:
                await self._emit_progress(
                    volume_id,
                    status="exporting",
                    percent=2.0,
                    stage="resolving",
                )

                async def on_chunk_progress(downloaded: int, total: int, stage: str) -> None:
                    if stage == "downloading_audio":
                        # Map audio download bytes smoothly across 5% -> 88% (or 5% -> 98% when no PDF)
                        span = 83.0 if book.get("has_pdf") or book.get("pdf_url") else 93.0
                        if total > 0:
                            pct = 5.0 + min(1.0, downloaded / total) * span
                        else:
                            # Unknown Content-Length: asymptotic curve up to 5 + span * 0.9
                            mb = downloaded / (1024 * 1024)
                            pct = 5.0 + (1.0 - (1.0 / (1.0 + mb / 25.0))) * (span * 0.9)
                    elif stage == "downloading_pdf":
                        if total > 0:
                            pct = 90.0 + min(1.0, downloaded / total) * 9.0
                        else:
                            pct = 94.0
                    else:
                        pct = 4.0

                    await self._emit_progress(
                        volume_id,
                        status="exporting",
                        percent=pct,
                        stage=stage,
                        downloaded_bytes=downloaded,
                        total_bytes=total,
                    )

                targets = await self.client.resolve_export_targets(
                    book=book,
                    book_folder=book_folder,
                    progress_cb=on_chunk_progress,
                )

                if not targets.export_supported:
                    restricted = self.db.update_export_progress(
                        volume_id,
                        status="restricted",
                        progress_percent=0.0,
                        stage="restricted",
                        error_message=targets.note or "Publisher restricts exporting this title.",
                    )
                    if restricted:
                        await self.events.publish("book_progress", restricted)
                    return

                book_folder.mkdir(parents=True, exist_ok=True)

                # Step 1: Download & finalize playable .m4a (or standard audio container)
                if targets.pre_downloaded_audio is not None:
                    final_audio = validate_and_finalize_audio(
                        targets.pre_downloaded_audio,
                        book_folder,
                        safe_title,
                        targets.audio_ext_hint,
                    )
                else:
                    audio_part = book_folder / f".{safe_title}.audio.part"
                    _dl_bytes, content_type, suggested_ext = await self.client.stream_download_to_part(
                        url=targets.audio_url,
                        part_path=audio_part,
                        stage="downloading_audio",
                        progress_cb=on_chunk_progress,
                    )
                    ext = choose_audio_extension(
                        suggested_ext or targets.audio_ext_hint, content_type
                    )
                    final_audio = validate_and_finalize_audio(audio_part, book_folder, safe_title, ext)

                audio_size = final_audio.stat().st_size

                # Step 2: Download & finalize accompanying PDF alongside the audio file (if available)
                final_pdf: Path | None = None
                note = targets.note

                if targets.pre_downloaded_pdf is not None:
                    final_pdf, pdf_note = validate_and_finalize_pdf(
                        targets.pre_downloaded_pdf, book_folder, safe_title
                    )
                    note = pdf_note or note
                elif targets.pdf_url:
                    await self._emit_progress(
                        volume_id,
                        status="exporting",
                        percent=90.0,
                        stage="downloading_pdf",
                        downloaded_bytes=0,
                        total_bytes=0,
                    )
                    pdf_part = book_folder / f".{safe_title}.pdf.part"
                    try:
                        await self.client.stream_download_to_part(
                            url=targets.pdf_url,
                            part_path=pdf_part,
                            stage="downloading_pdf",
                            progress_cb=on_chunk_progress,
                        )
                        final_pdf, pdf_note = validate_and_finalize_pdf(
                            pdf_part, book_folder, safe_title
                        )
                        note = pdf_note or note
                    except PermissionError:
                        raise
                    except Exception as pdf_exc:
                        if pdf_part.exists():
                            pdf_part.unlink()
                        note = f"Audio extracted; accompanying PDF download failed ({pdf_exc})."

                pdf_size = final_pdf.stat().st_size if final_pdf else 0
                completed = self.db.mark_extracted(
                    volume_id,
                    folder_name=book_folder.name,
                    audio_file=final_audio.name,
                    audio_size_bytes=audio_size,
                    pdf_file=final_pdf.name if final_pdf else "",
                    pdf_size_bytes=pdf_size,
                    note=note,
                )
                if completed:
                    await self.events.publish("book_progress", completed)
                    await self.events.publish("book_extracted", completed)

            except asyncio.CancelledError:
                self._cleanup_part_files(book_folder)
                raise
            except DrmProtectedError as drm_exc:
                self._cleanup_part_files(book_folder)
                restricted = self.db.update_export_progress(
                    volume_id,
                    status="restricted",
                    progress_percent=0.0,
                    stage="restricted",
                    error_message=str(drm_exc),
                )
                if restricted:
                    await self.events.publish("book_progress", restricted)
            except Exception as exc:
                self._cleanup_part_files(book_folder)
                failed = self.db.update_export_progress(
                    volume_id,
                    status="failed",
                    progress_percent=0.0,
                    stage="failed",
                    error_message=str(exc),
                )
                if failed:
                    await self.events.publish("book_progress", failed)

    async def _emit_progress(
        self,
        volume_id: str,
        *,
        status: str,
        percent: float,
        stage: str,
        downloaded_bytes: int = 0,
        total_bytes: int = 0,
    ) -> None:
        updated = self.db.update_export_progress(
            volume_id,
            status=status,
            progress_percent=percent,
            stage=stage,
            downloaded_bytes=downloaded_bytes,
            total_bytes=total_bytes,
        )
        if updated:
            await self.events.publish("book_progress", updated)

    @staticmethod
    def _cleanup_part_files(book_folder: Path) -> None:
        if not book_folder.exists():
            return
        for item in book_folder.iterdir():
            if item.is_file() and item.name.endswith(".part"):
                try:
                    item.unlink()
                except OSError:
                    pass
        try:
            if not any(book_folder.iterdir()):
                book_folder.rmdir()
        except OSError:
            pass
