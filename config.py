"""Local-first configuration for the VoC desktop application."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


FROZEN = bool(getattr(sys, "_MEIPASS", None))
RESOURCE_DIR = Path(sys._MEIPASS).resolve() if FROZEN else Path(__file__).resolve().parent
INSTALL_DIR = Path(sys.executable).resolve().parent if FROZEN else RESOURCE_DIR


def _resolve_data_dir() -> Path:
    """Use one stable per-user directory in packaged builds.

    VOC_DATA_DIR is intentionally supported for automated tests and portable
    troubleshooting. Source runs continue to use the repository's data folder.
    """
    override = os.environ.get("VOC_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if FROZEN:
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data).resolve() / "VoC-Platform" / "data"
        return INSTALL_DIR / "data"
    return RESOURCE_DIR / "data"


DATA_DIR = _resolve_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "voc.db"


def _database_is_empty(db_path: Path) -> bool:
    """True 表示库中还没有评论数据（仅建过空表，比如从无种子目录跑过一次自检/启动）。"""
    import sqlite3

    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='comments'"
            ).fetchone()
            if not row or not row[0]:
                return True
            return conn.execute("SELECT count(*) FROM comments").fetchone()[0] == 0
    except sqlite3.Error:
        return False


def _migrate_packaged_database_once() -> None:
    """Seed local storage from the release database without overwriting real data."""
    if not FROZEN:
        return
    packaged_db = INSTALL_DIR / "data" / "voc.db"
    if not packaged_db.is_file() or packaged_db.resolve() == DB_PATH.resolve():
        return
    if not DB_PATH.exists() or _database_is_empty(DB_PATH):
        shutil.copy2(packaged_db, DB_PATH)


_migrate_packaged_database_once()


class _Settings:
    DB_PATH: Path = DB_PATH

    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-pro"

    YTDLP_PATH: str = "yt-dlp"
    MAX_COMMENTS_PER_VIDEO: int = 500
    MAX_VIDEOS_PER_SEARCH: int = 10

    DEFAULT_COMPETITORS: list[str] = [
        "Blackview rugged phone review",
        "Ulefone Armor review",
        "Doogee rugged phone review",
        "Oukitel rugged phone review",
        "Unihertz rugged phone review",
    ]

    HOST: str = "127.0.0.1"
    PORT: int = 8000

    def __init__(self) -> None:
        self.GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
        env_file = INSTALL_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    if key.strip() == "GEMINI_API_KEY" and not self.GEMINI_API_KEY:
                        self.GEMINI_API_KEY = value.strip()


settings = _Settings()

