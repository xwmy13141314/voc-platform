"""Desktop entry point: a private FastAPI instance inside pywebview."""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.request import urlopen

# --- Fix for console=False: redirect null stdout/stderr to a log file ---
if getattr(sys, "frozen", False) and sys.stdout is None:
    _log_dir = Path(sys.executable).parent / "logs"
    _log_dir.mkdir(exist_ok=True)
    _log_file = open(_log_dir / "voc-platform.log", "a", encoding="utf-8")
    sys.stdout = _log_file
    sys.stderr = _log_file

from version import APP_NAME, APP_VERSION


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
_SERVER = None


def choose_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def start_server(port: int) -> None:
    global _SERVER
    import uvicorn
    from main import app

    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    _SERVER = uvicorn.Server(config)
    _SERVER.run()


def wait_for_server(port: int, instance_token: str, timeout: int = 25) -> dict | None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urlopen(f"http://{HOST}:{port}/api/health", timeout=1) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if (
                payload.get("status") == "ok"
                and payload.get("version") == APP_VERSION
                and payload.get("instance_token") == instance_token
            ):
                return payload
        except Exception:
            time.sleep(0.25)
    return None


def run_self_test() -> int:
    checks: dict[str, object] = {"version": APP_VERSION}
    try:
        import jiter
        from jiter import from_json
        import pydantic_core

        checks["jiter"] = bool(from_json(b'{"ok":true}').get("ok"))
        checks["jiter_path"] = str(Path(jiter.__file__).resolve())
        checks["pydantic_core"] = str(Path(pydantic_core.__file__).resolve())

        from config import settings
        from database import get_stats, init_db
        from main import STATIC_DIR

        init_db()
        checks["static"] = (STATIC_DIR / "index.html").is_file()
        checks["database_path"] = str(settings.DB_PATH.resolve())
        checks["stats"] = get_stats()
        checks["ok"] = bool(checks["jiter"] and checks["static"])
    except Exception as exc:
        checks["ok"] = False
        checks["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks.get("ok") else 1


def main() -> int:
    if "--self-test" in sys.argv:
        return run_self_test()

    instance_token = uuid.uuid4().hex
    port = choose_free_port()
    os.environ["VOC_INSTANCE_TOKEN"] = instance_token
    os.environ["VOC_SERVER_PORT"] = str(port)
    logger.info("正在启动 %s v%s（本机端口 %s）", APP_NAME, APP_VERSION, port)

    server_thread = threading.Thread(target=start_server, args=(port,), daemon=True)
    server_thread.start()
    health = wait_for_server(port, instance_token)
    if not health:
        logger.error("后端启动失败或实例身份校验未通过")
        return 1
    logger.info("后端就绪，数据库：%s", health.get("database_path", ""))

    try:
        import webview

        webview.create_window(
            f"{APP_NAME} v{APP_VERSION}",
            f"http://{HOST}:{port}",
            width=1280,
            height=860,
            min_size=(900, 600),
            text_select=True,
        )
        webview.start()
    except Exception as exc:
        logger.exception("桌面窗口启动失败：%s", exc)
        return 1
    finally:
        if _SERVER is not None:
            _SERVER.should_exit = True
            server_thread.join(timeout=5)

    logger.info("应用已关闭")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

