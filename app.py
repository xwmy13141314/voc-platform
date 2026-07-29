"""
VoC 痛点挖掘平台 — 桌面应用入口
pywebview 窗口 + 内嵌 FastAPI 后端
双击 exe 即可运行，无需安装 Python 环境
"""
import sys
import time
import threading
import logging
import socket

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8000


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) == 0


def start_server():
    """启动 FastAPI 后端（后台线程）"""
    import uvicorn
    from main import app
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


def wait_for_server(timeout: int = 20) -> bool:
    """等待服务就绪"""
    import urllib.request
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(f"http://{HOST}:{PORT}/api/stats", timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    logger.info("正在启动 VoC 痛点挖掘平台...")

    if is_port_in_use(PORT):
        logger.info("检测到服务已在运行，直接打开窗口")
    else:
        server_thread = threading.Thread(target=start_server, daemon=True)
        server_thread.start()
        if not wait_for_server():
            logger.error("后端服务启动失败")
            input("按回车键退出...")
            sys.exit(1)
        logger.info("后端服务就绪")

    try:
        import webview
        webview.create_window(
            "VoC 痛点挖掘平台",
            f"http://{HOST}:{PORT}",
            width=1280,
            height=860,
            min_size=(900, 600),
            text_select=True,
        )
        webview.start()
    except Exception as e:
        logger.error(f"窗口启动失败: {e}")
        input("按回车键退出...")
        sys.exit(1)

    logger.info("应用已关闭")
    sys.exit(0)


if __name__ == "__main__":
    main()
