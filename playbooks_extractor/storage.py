"""Filesystem organization (<Title> [<VolumeID>]/), file validation, and /data reconciliation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STANDARD_AUDIO_EXTENSIONS = (
    ".m4a",
    ".m4b",
    ".mp3",
    ".aac",
    ".ogg",
    ".opus",
    ".flac",
    ".wav",
)

UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE_RE = re.compile(r"\s+")
VOLUME_ID_CLEAN_RE = re.compile(r"[^A-Za-z0-9_-]")
EXTRACTED_FOLDER_RE = re.compile(r"^(?P<title>.+)\s+\[(?P<volume_id>[A-Za-z0-9_-]+)\]$")


def sanitize_title(title: str, max_length: int = 120) -> str:
    """Sanitize a book title into a safe, human-readable folder/file stem."""
    cleaned = UNSAFE_FILENAME_RE.sub(" - ", title or "")
    cleaned = WHITESPACE_RE.sub(" ", cleaned).strip(" .")
    # Collapse repeated separators created by colon replacements (e.g. "Title: Subtitle")
    cleaned = re.sub(r"(?:\s*-\s*){2,}", " - ", cleaned).strip(" .-")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .-")
    return cleaned or "Untitled Audiobook"


def sanitize_volume_id(volume_id: str) -> str:
    """Restrict volume ID to safe identifier characters (preventing path traversal)."""
    cleaned = VOLUME_ID_CLEAN_RE.sub("", (volume_id or "").strip())
    if not cleaned:
        raise ValueError(f"Invalid Google Play Books volume ID: {volume_id!r}")
    return cleaned


def build_book_folder_name(title: str, volume_id: str) -> str:
    """Format canonical '<Sanitized Title> [<VolumeID>]' directory name."""
    safe_title = sanitize_title(title)
    safe_id = sanitize_volume_id(volume_id)
    return f"{safe_title} [{safe_id}]"


def resolve_book_folder(data_dir: Path, title: str, volume_id: str) -> Path:
    """Return existing folder matching [<VolumeID>] if present, otherwise canonical folder path."""
    safe_id = sanitize_volume_id(volume_id)
    suffix = f"[{safe_id}]"
    if data_dir.exists():
        for entry in sorted(data_dir.iterdir()):
            if entry.is_dir() and entry.name.endswith(suffix):
                match = EXTRACTED_FOLDER_RE.match(entry.name)
                if match and match.group("volume_id") == safe_id:
                    return entry
    return data_dir / build_book_folder_name(title, safe_id)


def is_valid_pdf_bytes(header: bytes) -> bool:
    """Verify PDF magic header (%PDF-) and reject .acsm XML tickets or HTML error pages."""
    stripped = header.lstrip()
    return stripped.startswith(b"%PDF-")


def has_mp4_drm_encryption(header: bytes) -> bool:
    """Detect ISO BMFF CENC/DRM-encrypted audio sample entries (enca + sinf/cenc/cbcs/pssh)."""
    if len(header) < 16 or header[4:8] != b"ftyp":
        return False
    return b"enca" in header and any(
        marker in header for marker in (b"sinf", b"cenc", b"cbcs", b"pssh", b"schm")
    )


def is_valid_audio_bytes(header: bytes) -> bool:
    """Verify standard playable audio container signature (MP4/M4A ftyp, MP3 ID3/frame, Ogg, FLAC, RIFF)."""
    if len(header) < 8:
        return False
    # ISO BMFF (.m4a, .m4b, .mp4) has 'ftyp' at byte offset 4; reject CENC DRM-encrypted containers
    if header[4:8] == b"ftyp":
        return not has_mp4_drm_encryption(header)
    # MP3 with ID3v2 tag or MPEG audio sync word (0xFFE0 mask)
    if header.startswith(b"ID3") or (header[0] == 0xFF and (header[1] & 0xE0) == 0xE0):
        return True
    # Ogg (.ogg / .opus), FLAC, WAV (RIFF....WAVE), ADTS AAC
    if header.startswith((b"OggS", b"fLaC", b"RIFF")):
        return True
    return False


def is_valid_pdf_file(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 8:
        return False
    with path.open("rb") as handle:
        return is_valid_pdf_bytes(handle.read(64))


def is_valid_audio_file(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 16:
        return False
    with path.open("rb") as handle:
        return is_valid_audio_bytes(handle.read(65536))


@dataclass(frozen=True)
class DiskBookRecord:
    """On-disk representation of an extracted audiobook directory in /data."""

    volume_id: str
    title: str
    folder_name: str
    folder_path: Path
    audio_file: str
    audio_size_bytes: int
    pdf_file: str
    pdf_size_bytes: int
    extracted_at: str


def scan_data_directory(data_dir: Path) -> dict[str, DiskBookRecord]:
    """Scan /data for '<Title> [<VolumeID>]/' directories containing extracted audio (and optional PDF)."""
    discovered: dict[str, DiskBookRecord] = {}
    if not data_dir.exists():
        return discovered

    for entry in sorted(data_dir.iterdir()):
        if not entry.is_dir():
            continue
        match = EXTRACTED_FOLDER_RE.match(entry.name)
        if not match:
            continue

        title = match.group("title").strip()
        volume_id = match.group("volume_id").strip()

        audio_candidates: list[Path] = []
        pdf_candidates: list[Path] = []

        for child in sorted(entry.iterdir()):
            if not child.is_file() or child.name.endswith(".part"):
                continue
            suffix = child.suffix.lower()
            if suffix in STANDARD_AUDIO_EXTENSIONS and is_valid_audio_file(child):
                audio_candidates.append(child)
            elif suffix == ".pdf" and is_valid_pdf_file(child):
                pdf_candidates.append(child)

        if not audio_candidates:
            continue

        # Pick largest audio file as primary audiobook artifact (handles multi-file or single .m4a)
        primary_audio = max(audio_candidates, key=lambda p: p.stat().st_size)
        total_audio_bytes = sum(p.stat().st_size for p in audio_candidates)

        primary_pdf = max(pdf_candidates, key=lambda p: p.stat().st_size) if pdf_candidates else None
        pdf_bytes = primary_pdf.stat().st_size if primary_pdf else 0

        mtime = datetime.fromtimestamp(primary_audio.stat().st_mtime, tz=timezone.utc).isoformat()
        discovered[volume_id] = DiskBookRecord(
            volume_id=volume_id,
            title=title,
            folder_name=entry.name,
            folder_path=entry,
            audio_file=primary_audio.name,
            audio_size_bytes=total_audio_bytes,
            pdf_file=primary_pdf.name if primary_pdf else "",
            pdf_size_bytes=pdf_bytes,
            extracted_at=mtime,
        )

    return discovered
