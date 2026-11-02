"""SQLite persistence for library audiobooks, live export progress, and auth state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playbooks_extractor.storage import DiskBookRecord


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LibraryDatabase:
    """SQLite database tracking Google Play Books audiobooks and export progress."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS audiobooks (
                    volume_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    authors TEXT NOT NULL DEFAULT '',
                    cover_url TEXT NOT NULL DEFAULT '',
                    export_url TEXT NOT NULL DEFAULT '',
                    pdf_url TEXT NOT NULL DEFAULT '',
                    has_pdf INTEGER NOT NULL DEFAULT 0,
                    export_supported INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'available',
                    progress_percent REAL NOT NULL DEFAULT 0.0,
                    stage TEXT NOT NULL DEFAULT '',
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER NOT NULL DEFAULT 0,
                    folder_name TEXT NOT NULL DEFAULT '',
                    audio_file TEXT NOT NULL DEFAULT '',
                    audio_size_bytes INTEGER NOT NULL DEFAULT 0,
                    pdf_file TEXT NOT NULL DEFAULT '',
                    pdf_size_bytes INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    extracted_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    authenticated INTEGER NOT NULL DEFAULT 0,
                    checked_at TEXT,
                    account_hint TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT 'Session not verified yet.'
                );

                INSERT OR IGNORE INTO auth_state (id, authenticated, checked_at, account_hint, message)
                VALUES (1, 0, NULL, '', 'No session cookies imported yet.');

                CREATE TABLE IF NOT EXISTS schedule_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    cron_expression TEXT NOT NULL DEFAULT '0 2 * * *',
                    last_run_at TEXT,
                    last_status TEXT NOT NULL DEFAULT 'idle',
                    last_message TEXT NOT NULL DEFAULT 'Scheduled sync has not run yet.',
                    last_queued_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                """
            )

    def get_auth_state(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM auth_state WHERE id = 1").fetchone()
            if not row:
                return {
                    "authenticated": False,
                    "checked_at": None,
                    "account_hint": "",
                    "message": "No session cookies imported yet.",
                }
            return {
                "authenticated": bool(row["authenticated"]),
                "checked_at": row["checked_at"],
                "account_hint": row["account_hint"],
                "message": row["message"],
            }

    def set_auth_state(
        self,
        *,
        authenticated: bool,
        checked_at: str | None,
        account_hint: str = "",
        message: str = "",
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE auth_state
                SET authenticated = ?, checked_at = ?, account_hint = ?, message = ?
                WHERE id = 1
                """,
                (1 if authenticated else 0, checked_at, account_hint, message),
            )
        return self.get_auth_state()

    def get_schedule_state(
        self,
        *,
        default_cron: str = "0 2 * * *",
        default_enabled: bool = True,
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO schedule_state (
                    id, enabled, cron_expression, last_run_at, last_status,
                    last_message, last_queued_count, updated_at
                ) VALUES (1, ?, ?, NULL, 'idle', 'Scheduled sync has not run yet.', 0, ?)
                """,
                (1 if default_enabled else 0, default_cron, now),
            )
            row = conn.execute("SELECT * FROM schedule_state WHERE id = 1").fetchone()
            assert row is not None
            return {
                "enabled": bool(row["enabled"]),
                "cron_expression": row["cron_expression"],
                "last_run_at": row["last_run_at"],
                "last_status": row["last_status"],
                "last_message": row["last_message"],
                "last_queued_count": int(row["last_queued_count"]),
                "updated_at": row["updated_at"],
            }

    def save_schedule_config(
        self,
        *,
        enabled: bool,
        cron_expression: str,
    ) -> dict[str, Any]:
        now = _utc_now()
        self.get_schedule_state(default_cron=cron_expression, default_enabled=enabled)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE schedule_state
                SET enabled = ?, cron_expression = ?, updated_at = ?
                WHERE id = 1
                """,
                (1 if enabled else 0, cron_expression, now),
            )
        return self.get_schedule_state(default_cron=cron_expression, default_enabled=enabled)

    def record_schedule_run(
        self,
        *,
        last_run_at: str | None,
        last_status: str,
        last_message: str,
        last_queued_count: int = 0,
    ) -> dict[str, Any]:
        now = _utc_now()
        self.get_schedule_state()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE schedule_state
                SET last_run_at = ?,
                    last_status = ?,
                    last_message = ?,
                    last_queued_count = ?,
                    updated_at = ?
                WHERE id = 1
                """,
                (last_run_at, last_status, last_message, int(last_queued_count), now),
            )
        return self.get_schedule_state()

    def upsert_discovered_books(self, books: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Insert or update metadata for audiobooks discovered from Google Play Books."""
        now = _utc_now()
        with self._connect() as conn:
            for book in books:
                volume_id = str(book["volume_id"]).strip()
                title = str(book.get("title") or "Untitled Audiobook").strip()
                authors = str(book.get("authors") or "").strip()
                cover_url = str(book.get("cover_url") or "").strip()
                export_url = str(book.get("export_url") or "").strip()
                pdf_url = str(book.get("pdf_url") or "").strip()
                has_pdf = 1 if (book.get("has_pdf") or pdf_url) else 0
                export_supported = 0 if book.get("export_supported") is False else 1

                existing = conn.execute(
                    "SELECT * FROM audiobooks WHERE volume_id = ?", (volume_id,)
                ).fetchone()

                if existing is None:
                    initial_status = "available" if export_supported else "restricted"
                    conn.execute(
                        """
                        INSERT INTO audiobooks (
                            volume_id, title, authors, cover_url, export_url, pdf_url,
                            has_pdf, export_supported, status, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            volume_id,
                            title,
                            authors,
                            cover_url,
                            export_url,
                            pdf_url,
                            has_pdf,
                            export_supported,
                            initial_status,
                            now,
                        ),
                    )
                else:
                    # Preserve active, extracted, or confirmed DRM-restricted status; refresh metadata and URLs
                    cur_status = existing["status"]
                    if cur_status == "restricted" and existing["export_supported"] == 0:
                        export_supported = 0
                        new_status = "restricted"
                    elif cur_status in ("available", "restricted", "failed"):
                        new_status = "available" if export_supported else "restricted"
                    else:
                        new_status = cur_status
                    conn.execute(
                        """
                        UPDATE audiobooks
                        SET title = ?,
                            authors = CASE WHEN ? != '' THEN ? ELSE authors END,
                            cover_url = CASE WHEN ? != '' THEN ? ELSE cover_url END,
                            export_url = CASE WHEN ? != '' THEN ? ELSE export_url END,
                            pdf_url = CASE WHEN ? != '' THEN ? ELSE pdf_url END,
                            has_pdf = CASE WHEN ? = 1 THEN 1 ELSE has_pdf END,
                            export_supported = ?,
                            status = ?,
                            updated_at = ?
                        WHERE volume_id = ?
                        """,
                        (
                            title,
                            authors,
                            authors,
                            cover_url,
                            cover_url,
                            export_url,
                            export_url,
                            pdf_url,
                            pdf_url,
                            has_pdf,
                            export_supported,
                            new_status,
                            now,
                            volume_id,
                        ),
                    )
        return self.list_books()

    def prune_stale_unextracted(self, keep_volume_ids: set[str]) -> int:
        """Delete stale unextracted DB rows that are absent from a live library discovery pass."""
        if not keep_volume_ids:
            return 0
        placeholders = ",".join("?" for _ in keep_volume_ids)
        params = tuple(keep_volume_ids)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                DELETE FROM audiobooks
                WHERE status != 'extracted'
                  AND volume_id NOT IN ({placeholders})
                """,
                params,
            )
            return cursor.rowcount

    def reconcile_with_disk(self, disk_records: dict[str, DiskBookRecord]) -> list[dict[str, Any]]:
        """Sync SQLite audiobook state with extracted folders present in /data."""
        now = _utc_now()
        with self._connect() as conn:
            existing_rows = {
                row["volume_id"]: row
                for row in conn.execute("SELECT * FROM audiobooks").fetchall()
            }

            # 1. Mark or insert books found on disk as extracted
            for volume_id, rec in disk_records.items():
                row = existing_rows.get(volume_id)
                has_pdf = 1 if rec.pdf_file else 0
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO audiobooks (
                            volume_id, title, has_pdf, status, progress_percent, stage,
                            folder_name, audio_file, audio_size_bytes, pdf_file, pdf_size_bytes,
                            extracted_at, updated_at
                        ) VALUES (?, ?, ?, 'extracted', 100.0, 'completed', ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            volume_id,
                            rec.title,
                            has_pdf,
                            rec.folder_name,
                            rec.audio_file,
                            rec.audio_size_bytes,
                            rec.pdf_file,
                            rec.pdf_size_bytes,
                            rec.extracted_at,
                            now,
                        ),
                    )
                elif row["status"] not in ("queued", "exporting"):
                    conn.execute(
                        """
                        UPDATE audiobooks
                        SET status = 'extracted',
                            progress_percent = 100.0,
                            stage = 'completed',
                            has_pdf = CASE WHEN ? = 1 THEN 1 ELSE has_pdf END,
                            folder_name = ?,
                            audio_file = ?,
                            audio_size_bytes = ?,
                            pdf_file = ?,
                            pdf_size_bytes = ?,
                            error_message = CASE WHEN status = 'extracted' THEN error_message ELSE '' END,
                            extracted_at = COALESCE(extracted_at, ?),
                            updated_at = ?
                        WHERE volume_id = ?
                        """,
                        (
                            has_pdf,
                            rec.folder_name,
                            rec.audio_file,
                            rec.audio_size_bytes,
                            rec.pdf_file,
                            rec.pdf_size_bytes,
                            rec.extracted_at,
                            now,
                            volume_id,
                        ),
                    )

            # 2. If a book was previously marked 'extracted' but its folder was removed from /data, reset to 'available'
            for volume_id, row in existing_rows.items():
                if row["status"] == "extracted" and volume_id not in disk_records:
                    conn.execute(
                        """
                        UPDATE audiobooks
                        SET status = CASE WHEN export_supported = 1 THEN 'available' ELSE 'restricted' END,
                            progress_percent = 0.0,
                            stage = '',
                            downloaded_bytes = 0,
                            total_bytes = 0,
                            folder_name = '',
                            audio_file = '',
                            audio_size_bytes = 0,
                            pdf_file = '',
                            pdf_size_bytes = 0,
                            extracted_at = NULL,
                            updated_at = ?
                        WHERE volume_id = ?
                        """,
                        (now, volume_id),
                    )

        return self.list_books()

    def reset_interrupted_exports(self) -> None:
        """Reset any stale 'queued' or 'exporting' jobs left from an unclean process restart."""
        now = _utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE audiobooks
                SET status = 'available',
                    progress_percent = 0.0,
                    stage = '',
                    downloaded_bytes = 0,
                    total_bytes = 0,
                    error_message = 'Interrupted by server restart; ready to retry.',
                    updated_at = ?
                WHERE status IN ('queued', 'exporting')
                """,
                (now,),
            )

    def update_export_progress(
        self,
        volume_id: str,
        *,
        status: str,
        progress_percent: float,
        stage: str,
        downloaded_bytes: int = 0,
        total_bytes: int = 0,
        error_message: str = "",
    ) -> dict[str, Any] | None:
        now = _utc_now()
        clamped = round(max(0.0, min(100.0, float(progress_percent))), 1)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE audiobooks
                SET status = ?,
                    export_supported = CASE WHEN ? = 'restricted' THEN 0 ELSE export_supported END,
                    progress_percent = ?,
                    stage = ?,
                    downloaded_bytes = ?,
                    total_bytes = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE volume_id = ?
                """,
                (
                    status,
                    status,
                    clamped,
                    stage,
                    int(downloaded_bytes),
                    int(total_bytes),
                    error_message,
                    now,
                    volume_id,
                ),
            )
        return self.get_book(volume_id)

    def mark_extracted(
        self,
        volume_id: str,
        *,
        folder_name: str,
        audio_file: str,
        audio_size_bytes: int,
        pdf_file: str = "",
        pdf_size_bytes: int = 0,
        note: str = "",
    ) -> dict[str, Any] | None:
        now = _utc_now()
        has_pdf = 1 if pdf_file else 0
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE audiobooks
                SET status = 'extracted',
                    progress_percent = 100.0,
                    stage = 'completed',
                    downloaded_bytes = ?,
                    total_bytes = ?,
                    has_pdf = CASE WHEN ? = 1 THEN 1 ELSE has_pdf END,
                    folder_name = ?,
                    audio_file = ?,
                    audio_size_bytes = ?,
                    pdf_file = ?,
                    pdf_size_bytes = ?,
                    error_message = ?,
                    extracted_at = ?,
                    updated_at = ?
                WHERE volume_id = ?
                """,
                (
                    audio_size_bytes + pdf_size_bytes,
                    audio_size_bytes + pdf_size_bytes,
                    has_pdf,
                    folder_name,
                    audio_file,
                    audio_size_bytes,
                    pdf_file,
                    pdf_size_bytes,
                    note,
                    now,
                    now,
                    volume_id,
                ),
            )
        return self.get_book(volume_id)

    def get_book(self, volume_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM audiobooks WHERE volume_id = ?", (volume_id,)
            ).fetchone()
            return self._row_to_dict(row) if row else None

    def list_books(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM audiobooks
                ORDER BY
                    CASE status
                        WHEN 'exporting' THEN 0
                        WHEN 'queued' THEN 1
                        WHEN 'extracted' THEN 2
                        WHEN 'available' THEN 3
                        WHEN 'failed' THEN 4
                        ELSE 5
                    END,
                    LOWER(title) ASC
                """
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "volume_id": row["volume_id"],
            "title": row["title"],
            "authors": row["authors"],
            "cover_url": row["cover_url"],
            "export_url": row["export_url"],
            "pdf_url": row["pdf_url"],
            "has_pdf": bool(row["has_pdf"]),
            "export_supported": bool(row["export_supported"]),
            "status": row["status"],
            "progress_percent": float(row["progress_percent"]),
            "stage": row["stage"],
            "downloaded_bytes": int(row["downloaded_bytes"]),
            "total_bytes": int(row["total_bytes"]),
            "folder_name": row["folder_name"],
            "audio_file": row["audio_file"],
            "audio_size_bytes": int(row["audio_size_bytes"]),
            "pdf_file": row["pdf_file"],
            "pdf_size_bytes": int(row["pdf_size_bytes"]),
            "error_message": row["error_message"],
            "extracted_at": row["extracted_at"],
            "updated_at": row["updated_at"],
        }
