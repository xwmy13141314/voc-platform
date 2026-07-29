"""
VoC 痛点挖掘平台 — 全局配置
MVP 阶段使用 SQLite + Gemini + yt-dlp
"""
from pathlib import Path
import sys

# 打包后数据放在 exe 同级目录，开发时放在脚本所在目录
if getattr(sys, '_MEIPASS', None):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

# 数据目录
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


class _Settings:
    """配置类 — 从环境变量或默认值读取"""

    # 数据库路径
    DB_PATH: Path = DATA_DIR / "voc.db"

    # Gemini API
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-pro"

    # YouTube 抓取配置
    YTDLP_PATH: str = "yt-dlp"
    MAX_COMMENTS_PER_VIDEO: int = 500
    MAX_VIDEOS_PER_SEARCH: int = 10

    # 默认竞品搜索关键词
    DEFAULT_COMPETITORS: list = [
        "Blackview rugged phone review",
        "Ulefone Armor review",
        "Doogee rugged phone review",
        "Oukitel rugged phone review",
        "Unihertz rugged phone review",
    ]

    # 服务器
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    def __init__(self):
        import os
        self.GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
        # 尝试从 .env 文件读取
        env_file = BASE_DIR / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if key == "GEMINI_API_KEY" and not self.GEMINI_API_KEY:
                        self.GEMINI_API_KEY = value


settings = _Settings()
