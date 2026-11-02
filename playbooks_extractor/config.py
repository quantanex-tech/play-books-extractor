"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Application runtime settings."""

    data_dir: Path
    config_dir: Path
    base_url: str = "https://play.google.com"
    books_api_url: str = "https://www.googleapis.com/books/v1/volumes"
    use_browser: str = "auto"  # "auto" | "always" | "never"
    export_quality: str = "high"  # "high" | "standard"
    schedule_enabled: bool = True
    schedule_cron: str = "0 2 * * *"
    host: str = "0.0.0.0"
    port: int = 8080
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    @property
    def cookies_path(self) -> Path:
        return self.config_dir / "cookies.json"

    @property
    def db_path(self) -> Path:
        return self.config_dir / "library.db"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.environ.get("PLAYBOOKS_DATA_DIR", "/data")).expanduser().resolve()
        config_dir = Path(os.environ.get("PLAYBOOKS_CONFIG_DIR", "/config")).expanduser().resolve()
        base_url = os.environ.get("PLAYBOOKS_BASE_URL", "https://play.google.com").rstrip("/")
        books_api_url = os.environ.get(
            "PLAYBOOKS_BOOKS_API_URL", "https://www.googleapis.com/books/v1/volumes"
        ).rstrip("/")
        use_browser = os.environ.get("PLAYBOOKS_USE_BROWSER", "auto").strip().lower()
        raw_quality = os.environ.get("PLAYBOOKS_EXPORT_QUALITY", "high").strip().lower()
        export_quality = "standard" if raw_quality == "standard" else "high"
        raw_sched_enabled = os.environ.get("PLAYBOOKS_SCHEDULE_ENABLED", "true").strip().lower()
        schedule_enabled = raw_sched_enabled not in ("false", "0", "no", "off")
        schedule_cron = os.environ.get("PLAYBOOKS_SCHEDULE_CRON", "0 2 * * *").strip() or "0 2 * * *"
        host = os.environ.get("PLAYBOOKS_HOST", "0.0.0.0")
        port = int(os.environ.get("PLAYBOOKS_PORT", "8080"))
        return cls(
            data_dir=data_dir,
            config_dir=config_dir,
            base_url=base_url,
            books_api_url=books_api_url,
            use_browser=use_browser,
            export_quality=export_quality,
            schedule_enabled=schedule_enabled,
            schedule_cron=schedule_cron,
            host=host,
            port=port,
        )
