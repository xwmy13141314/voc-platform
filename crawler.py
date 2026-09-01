"""
多平台评论抓取模块
支持: YouTube (yt-dlp), Reddit (PRAW OAuth + Cookie), Instagram (Instaloader),
      TikTok (元数据), Facebook (facebook-scraper), AliExpress (公开评论 API)

v2.0: 新平台实现逐步迁移到 sources/ 包（quality_filter / aliexpress），
      本文件保留存量平台与统一入库链路。
"""
import re
import uuid
import logging
import json
import time
import html
import xml.etree.ElementTree as ET
from yt_dlp import YoutubeDL
from config import settings
from database import get_db, insert_comment, insert_brand
from sources.common import clean_comment, detect_language
from sources.quality_filter import evaluate_comment, load_filter_config
from sources.aliexpress import (
    AliExpressError,
    extract_comments_aliexpress,
    search_products_aliexpress,
)

logger = logging.getLogger(__name__)

# HTTP 请求公共头
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9",
}

# Reddit 请求头。JSON API 是旧版本实际使用并验证过的主路径；RSS 只作为备用。
_REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/atom+xml, application/rss+xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
# Atom 命名空间
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
# Reddit 请求间隔（秒），避免连续请求触发限流，同时不让桌面端长时间假死。
_REDDIT_DELAY = 3.0
# Reddit 主站及兼容备用域名。部分网络环境会只允许其中一个域名。
_REDDIT_BASE = "https://www.reddit.com"
_REDDIT_BASES = ("https://www.reddit.com", "https://old.reddit.com")


class RedditFetchError(RuntimeError):
    """Reddit 请求失败，必须向任务层传递而不是静默返回空结果。"""

    def __init__(self, operation: str, details):
        if isinstance(details, str):
            details = [details]
        self.operation = operation
        self.details = [str(item) for item in (details or [])]
        suffix = "; ".join(self.details) or "未知错误"
        super().__init__(f"Reddit {operation} 失败: {suffix}")




def _reddit_block_hint(errors):
    """If ALL error strings indicate HTTP 403, return a user-facing hint."""
    if not errors:
        return None
    blocked = all("403" in str(e) for e in errors)
    if not blocked:
        return None
    has_oauth = _has_reddit_oauth()
    has_cookie = _has_reddit_cookie_auth()
    if has_oauth or has_cookie:
        return None
    return (
        "Reddit 已封禁当前 IP（所有匿名请求均返回 403）。\n"
        "推荐方案 - PRAW 官方 API（最可靠）：\n"
        "1. 浏览器打开 https://www.reddit.com/prefs/apps\n"
        "2. 页面底部找到 'create another app...' → 选择 'script' 类型\n"
        "3. 名称填 voc-platform，redirect URI 填 http://localhost\n"
        "4. 创建后，应用名称下方的短字符串是 Client ID，secret 旁是 Client Secret\n"
        "5. 在「设置 → Reddit OAuth 配置」中填入 Client ID 和 Client Secret 并保存\n"
        "6. PRAW 会自动处理 OAuth 认证、速率限制和分页\n\n"
        "备选方案B - Cookie 认证：\n"
        "1. 在浏览器中登录 Reddit 账号\n"
        "2. 在「设置 → Reddit Cookie 配置」中选择浏览器，系统自动提取登录 cookies"
    )


# ==================== 文本处理工具 ====================
# clean_comment / detect_language 已迁移到 sources/common.py（上方导入），
# 此处保留模块级名字以兼容既有 import（from crawler import clean_comment）。


# ==================== 数据库操作 ====================

def save_video(video: dict, brand_id: str, platform: str = "youtube") -> str:
    """保存视频/帖子信息到数据库，返回 video_id"""
    conn = get_db()
    video_db_id = str(uuid.uuid4())
    external_id = str(video.get("external_id") or video.get("video_id", ""))
    # 物理主键使用平台命名空间，避免不同平台复用同一短 ID。
    storage_id = f"{platform}:{external_id}"
    source_url = video.get("source_url") or platform_source_url(platform, external_id, video)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO videos
                (id, video_id, platform, external_id, title, channel, view_count,
                 comment_count, published_at, source_url, brand_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            video_db_id,
            storage_id,
            platform,
            external_id,
            video.get("title", ""),
            video.get("channel", ""),
            video.get("view_count", 0),
            video.get("comment_count", 0),
            video.get("published_at", ""),
            source_url,
            brand_id,
        ))
        conn.commit()
    finally:
        conn.close()

    conn = get_db()
    row = conn.execute(
        "SELECT id FROM videos WHERE video_id = ?", (storage_id,)
    ).fetchone()
    conn.close()
    return row["id"] if row else video_db_id


def platform_source_url(platform: str, external_id: str, item: dict | None = None) -> str:
    """返回可回溯的公开来源 URL；抓取器可通过 item 覆盖更精确的链接。"""
    item = item or {}
    if item.get("_url"):
        return item["_url"]
    if platform == "youtube":
        return f"https://www.youtube.com/watch?v={external_id}"
    if platform == "reddit":
        return item.get("_url") or f"https://www.reddit.com/comments/{external_id}/"
    if platform == "instagram":
        return f"https://www.instagram.com/p/{external_id}/"
    if platform == "tiktok":
        return f"https://www.tiktok.com/video/{external_id}"
    if platform == "facebook":
        return f"https://www.facebook.com/{external_id}"
    if platform == "aliexpress":
        return f"https://www.aliexpress.com/item/{external_id}.html"
    return ""


def _store_comments(comments: list[dict], video_db_id: str, brand_id: str, platform: str) -> tuple[int, int, int]:
    """存储评论到数据库（含 v2.0 质量过滤），返回 (通过数, 新增数, 被过滤数)。

    被过滤的评论同样入库（filtered=1 + filter_reason），仅不进入分析队列；
    清洗后完全无内容（纯链接被去除）的过滤评论不再占用存储。
    """
    cfg = load_filter_config()
    total = 0
    new_count = 0
    filtered_count = 0
    for c in comments:
        text = c.get("content", "").strip()
        if not text:
            continue
        cleaned = clean_comment(text)
        verdict = evaluate_comment(cleaned, platform, c.get("like_count", 0),
                                   depth=c.get("depth", 0), cfg=cfg, raw_content=text)
        if not verdict.passed:
            if not cleaned and verdict.reason != "link_only":
                continue
            filtered_count += 1
        elif not cleaned:
            # 过滤关闭时纯链接评论清洗后为空，无存储价值
            continue
        lang = c.get("language") or (detect_language(cleaned) if cleaned else "")
        meta = c.get("meta")
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        is_new = insert_comment({
            "platform": platform,
            "original_id": c["original_id"],
            "content": text,
            "content_clean": cleaned,
            "video_id": video_db_id,
            "brand_id": brand_id,
            "author": c.get("author", ""),
            "like_count": c.get("like_count", 0),
            "posted_at": c.get("posted_at", ""),
            "source_url": c.get("source_url") or platform_source_url(
                platform, c.get("original_id", ""), c
            ),
            "language": lang,
            "parent_id": c.get("parent_id"),
            "depth": c.get("depth", 0),
            "quality_score": verdict.quality_score,
            "filtered": 0 if verdict.passed else 1,
            "filter_reason": verdict.reason or None,
            "meta_json": meta_json,
        })
        if verdict.passed:
            total += 1
            if is_new:
                new_count += 1
    if filtered_count:
        logger.info("[%s] 质量过滤: %d/%d 条评论被标记过滤",
                    platform, filtered_count, len(comments))
    return total, new_count, filtered_count


# ==================== YouTube ====================

def search_videos_youtube(keyword: str, limit: int = 10) -> list[dict]:
    """用 yt-dlp 搜索 YouTube 视频"""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    videos = []
    try:
        with YoutubeDL(opts) as ydl:
            result = ydl.extract_info(f"ytsearch{limit}:{keyword}", download=False)
            for entry in result.get("entries", []):
                vid = entry.get("id", "")
                if not vid:
                    continue
                videos.append({
                "video_id": vid,
                    "title": entry.get("title", ""),
                    "channel": entry.get("channel", entry.get("uploader", "")),
                    "view_count": entry.get("view_count", 0) or 0,
                    "comment_count": entry.get("comment_count", 0) or 0,
                    "published_at": entry.get("upload_date", ""),
                    "source_url": f"https://www.youtube.com/watch?v={vid}",
                })
    except Exception as e:
        logger.error(f"YouTube 搜索失败: {keyword} - {e}")
        raise RuntimeError(f"YouTube 搜索失败: {e}") from e
    return videos


def extract_comments_youtube(video_id: str, max_comments: int = 500) -> list[dict]:
    """提取 YouTube 视频评论"""
    opts = {"quiet": True, "no_warnings": True, "getcomments": True, "skip_download": True}
    comments = []
    try:
        with YoutubeDL(opts) as ydl:
            result = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
            for c in (result.get("comments") or [])[:max_comments]:
                text = c.get("text", "").strip()
                if not text:
                    continue
                comments.append({
                    "original_id": str(c.get("id", uuid.uuid4())),
                    "content": text,
                    "author": c.get("author", ""),
                    "like_count": c.get("like_count", 0) or 0,
                    "posted_at": str(c.get("timestamp", "")),
                    "source_url": f"https://www.youtube.com/watch?v={video_id}&lc={c.get('id', '')}",
                })
    except Exception as e:
        logger.error(f"YouTube 评论提取失败: {video_id} - {e}")
        raise RuntimeError(f"YouTube 评论提取失败: {e}") from e
    return comments


# ==================== Reddit (RSS/Atom) ====================

# ---- Reddit OAuth (authenticated API, primary path) ----

_REDDIT_OAUTH_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_REDDIT_OAUTH_API_BASE = "https://oauth.reddit.com"
_reddit_oauth_state: dict = {"token": None, "expiry": 0.0}


def _build_reddit_ua(username: str = "") -> str:
    """Reddit requires User-Agent format: platform:app:version (by /u/username)"""
    try:
        from version import APP_VERSION
    except Exception:
        APP_VERSION = "0.8.0"
    uname = username.strip()
    if uname:
        return f"windows:voc-pain-point-miner:v{APP_VERSION} (by /u/{uname})"
    return f"windows:voc-pain-point-miner:v{APP_VERSION}"


def _get_reddit_oauth_config() -> dict:
    """Read Reddit OAuth credentials from database settings."""
    try:
        from database import get_setting
        return {
            "client_id": get_setting("reddit_client_id", ""),
            "client_secret": get_setting("reddit_client_secret", ""),
            "username": get_setting("reddit_username", ""),
            "password": get_setting("reddit_password", ""),
        }
    except Exception:
        return {"client_id": "", "client_secret": "", "username": "", "password": ""}


def _has_reddit_oauth() -> bool:
    """Check if Reddit OAuth credentials are configured."""
    cfg = _get_reddit_oauth_config()
    return bool(cfg["client_id"] and cfg["client_secret"])


def _get_reddit_oauth_token():
    """Acquire or refresh OAuth token for Reddit API."""
    global _reddit_oauth_state
    if _reddit_oauth_state["token"] and time.time() < _reddit_oauth_state["expiry"] - 60:
        return _reddit_oauth_state["token"]
    cfg = _get_reddit_oauth_config()
    if not (cfg["client_id"] and cfg["client_secret"]):
        return None
    import requests as _requests
    auth = _requests.auth.HTTPBasicAuth(cfg["client_id"], cfg["client_secret"])
    ua = _build_reddit_ua(cfg.get("username", ""))
    if cfg["username"] and cfg["password"]:
        data = {"grant_type": "password", "username": cfg["username"], "password": cfg["password"]}
    else:
        data = {"grant_type": "client_credentials"}
    try:
        resp = _requests.post(_REDDIT_OAUTH_TOKEN_URL, auth=auth, data=data,
                              headers={"User-Agent": ua}, timeout=15)
        if resp.status_code != 200:
            logger.error("Reddit OAuth token HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        token_data = resp.json()
        _reddit_oauth_state = {
            "token": token_data.get("access_token"),
            "expiry": time.time() + token_data.get("expires_in", 3600),
        }
        logger.info("Reddit OAuth token acquired (expires in %ss)", token_data.get("expires_in"))
        return _reddit_oauth_state["token"]
    except Exception as exc:
        logger.error("Reddit OAuth token failed: %s", exc)
        return None


def _reddit_oauth_rate_limit_wait(resp, log_tag: str = "") -> None:
    """解析 x-ratelimit-* 头，配额将耗尽时等待窗口重置，避免下一次请求吃 429。"""
    try:
        remaining = float(resp.headers.get("x-ratelimit-remaining", ""))
    except (TypeError, ValueError):
        return
    if remaining > 1.0:
        return
    try:
        reset = float(resp.headers.get("x-ratelimit-reset", "60"))
    except (TypeError, ValueError):
        reset = 60.0
    wait = max(1.0, min(reset, 120.0))
    logger.info("Reddit OAuth 配额剩余 %.0f [%s]，等待 %.0fs 至窗口重置", remaining, log_tag, wait)
    time.sleep(wait)


def _reddit_oauth_get(path: str, params=None, log_tag: str = ""):
    """Authenticated GET to oauth.reddit.com with rate-limit header handling and
    exponential backoff on 429."""
    import requests as _requests

    token = _get_reddit_oauth_token()
    if not token:
        return None
    cfg = _get_reddit_oauth_config()
    ua = _build_reddit_ua(cfg.get("username", ""))
    url = f"{_REDDIT_OAUTH_API_BASE}{path}"
    backoff = 1.0
    for attempt in range(4):
        headers = {"Authorization": f"Bearer {token}", "User-Agent": ua}
        try:
            resp = _requests.get(url, params=params, headers=headers, timeout=20)
            _reddit_oauth_rate_limit_wait(resp, log_tag)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 401 and attempt == 0:
                logger.info("Reddit OAuth 401, refreshing token [%s]", log_tag)
                _reddit_oauth_state.update(token=None, expiry=0.0)
                token = _get_reddit_oauth_token()
                if token:
                    continue
                return None
            if resp.status_code == 429 and attempt < 3:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = max(1.0, min(float(retry_after), 60.0)) if retry_after else 0.0
                except (TypeError, ValueError):
                    wait = 0.0
                wait = max(wait, backoff)
                logger.warning(
                    "Reddit OAuth 429 [%s]，退避 %.1fs 后重试 (attempt %d/4)",
                    log_tag, wait, attempt + 1,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, 60.0)
                continue
            logger.warning("Reddit OAuth HTTP %s [%s]", resp.status_code, log_tag)
            return None
        except Exception as exc:
            logger.warning("Reddit OAuth request error [%s]: %s", log_tag, exc)
            if attempt < 3:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            return None
    return None


# ---- PRAW (Official Reddit API Wrapper, 最高优先级) ----
# PRAW 是 Reddit 官方推荐的 Python API 封装库，自动处理 OAuth 认证、
# 速率限制、分页和错误重试，比手动 raw requests 更可靠。
_praw_instance = None


def _get_praw_instance():
    """创建或复用 PRAW Reddit 实例。需要 OAuth 凭据 (client_id + client_secret)。"""
    global _praw_instance
    if _praw_instance is not None:
        return _praw_instance

    cfg = _get_reddit_oauth_config()
    if not (cfg["client_id"] and cfg["client_secret"]):
        return None

    try:
        import praw
        username = cfg.get("username") or None
        password = cfg.get("password") or None
        _praw_instance = praw.Reddit(
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            user_agent=_build_reddit_ua(cfg.get("username", "")),
            username=username,
            password=password,
            check_for_updates=False,
        )
        _praw_instance.read_only = not (username and password)
        logger.info("PRAW 实例已创建 (read_only=%s)", _praw_instance.read_only)
        return _praw_instance
    except Exception as exc:
        logger.error("PRAW 实例创建失败: %s", exc)
        return None


def _reddit_reset_praw():
    """重置 PRAW 实例，下次使用时重新创建。"""
    global _praw_instance
    _praw_instance = None


def _praw_search_posts(keyword: str, limit: int = 25) -> list[dict]:
    """使用 PRAW 搜索 Reddit 帖子。返回与 _reddit_post_from_json_v2 兼容的格式。"""
    reddit = _get_praw_instance()
    if reddit is None:
        raise RedditFetchError("praw_search", "PRAW 未配置，需要 OAuth 凭据 (client_id + client_secret)")

    posts = []
    try:
        results = reddit.subreddit("all").search(keyword, limit=limit, sort="relevance")
        for submission in results:
            permalink = getattr(submission, "permalink", "") or ""
            source_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else ""
            subreddit_name = ""
            try:
                subreddit_name = submission.subreddit.display_name
            except Exception:
                pass
            posts.append({
                "video_id": submission.id,
                "title": submission.title or "",
                "content": submission.selftext or "",
                "channel": f"r/{subreddit_name}" if subreddit_name else "",
                "view_count": submission.score or 0,
                "comment_count": submission.num_comments or 0,
                "published_at": _reddit_date_v2(submission.created_utc),
                "_subreddit": subreddit_name,
                "_url": source_url,
                "source_url": source_url,
            })
        logger.info("PRAW 搜索 '%s' -> %d 结果", keyword[:30], len(posts))
        return posts
    except Exception as exc:
        raise RedditFetchError("praw_search", f"{type(exc).__name__}: {exc}")


def _praw_extract_comments(post_id: str, max_comments: int = 500) -> list[dict]:
    """使用 PRAW 提取 Reddit 帖子评论（含 parent_id/depth 楼中楼血缘）。"""
    reddit = _get_praw_instance()
    if reddit is None:
        raise RedditFetchError("praw_comments", "PRAW 未配置，需要 OAuth 凭据")

    comments = []
    try:
        submission = reddit.submission(id=post_id)
        # replace_more(limit=0) 跳过 "load more comments" 占位符，避免额外请求
        submission.comments.replace_more(limit=0)

        # .list() 为树序（父先于子），一边遍历一边累计深度；
        # 被过滤的 [deleted] 评论也记录深度，保证其子回复血缘不断
        depth_map: dict[str, int] = {}
        for comment in submission.comments.list():
            parent_full = getattr(comment, "parent_id", "") or ""
            if parent_full.startswith("t1_"):
                parent_id = parent_full[3:]
                depth = depth_map.get(parent_id, 0) + 1
            else:
                parent_id = None
                depth = 0
            depth_map[str(comment.id)] = depth

            body = (comment.body or "").strip()
            if not body or body in {"[deleted]", "[removed]"}:
                continue
            permalink = getattr(comment, "permalink", "") or ""
            source_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else ""
            author_str = ""
            try:
                author_str = str(comment.author) if comment.author else ""
            except Exception:
                pass
            comments.append({
                "original_id": str(comment.id),
                "content": body,
                "author": author_str,
                "like_count": comment.score or 0,
                "posted_at": _reddit_date_v2(comment.created_utc),
                "source_url": source_url,
                "parent_id": parent_id,
                "depth": depth,
            })
            if len(comments) >= max_comments:
                break

        logger.info("PRAW 评论 %s -> %d 条", post_id, len(comments))
        return comments
    except Exception as exc:
        raise RedditFetchError(f"praw_comments {post_id}", f"{type(exc).__name__}: {exc}")


# ---- Reddit Cookie 认证（方案B：嵌入 rdt-cli 核心逻辑）----
# 通过 browser_cookie3 从用户浏览器提取 Reddit 登录 cookies，
# 或使用用户手动粘贴的 cookies，绕过 IP 封禁。
# Chrome/Edge 运行时会锁定 cookie 文件，需要关闭浏览器后提取并缓存。

_reddit_cookie_session = None
_reddit_cookie_session_ready = False


def _reddit_get_cookie_config() -> dict:
    """从数据库读取 Reddit Cookie 配置。"""
    try:
        from database import get_setting
        return {
            "method": get_setting("reddit_cookie_method", "browser"),  # browser / manual
            "browser": get_setting("reddit_browser", "chrome"),  # chrome / firefox / edge / opera / brave
            "cookies": get_setting("reddit_cookies", ""),  # 手动粘贴的 cookies
            "cached_cookies": get_setting("reddit_cached_cookies", ""),  # 缓存的浏览器提取 cookies
        }
    except Exception:
        return {"method": "browser", "browser": "chrome", "cookies": "", "cached_cookies": ""}


def _has_reddit_cookie_auth() -> bool:
    """检查是否配置了 Cookie 认证。"""
    cfg = _reddit_get_cookie_config()
    if cfg["method"] == "manual":
        return bool(cfg["cookies"].strip())
    # browser 模式：有缓存 cookies 或可以尝试浏览器提取
    return bool(cfg.get("cached_cookies", "").strip()) or True


def _reddit_extract_browser_cookies(browser: str = "chrome") -> dict | None:
    """使用 browser_cookie3 从用户浏览器提取 Reddit cookies。

    支持: chrome, firefox, edge, opera, brave
    返回 cookies dict 或 None（提取失败时）。
    注意: Chrome/Edge 在 Windows 上运行时会锁定 cookie 文件，需要关闭浏览器。
    """
    try:
        import browser_cookie3 as bc3
    except ImportError:
        logger.error("browser_cookie3 未安装，无法提取浏览器 cookies")
        return None

    browser = browser.lower().strip()

    _extractors = {
        "chrome": lambda: bc3.chrome(domain_name="reddit.com"),
        "firefox": lambda: bc3.firefox(domain_name="reddit.com"),
        "edge": lambda: bc3.edge(domain_name="reddit.com"),
        "opera": lambda: bc3.opera(domain_name="reddit.com"),
        "brave": lambda: bc3.brave(domain_name="reddit.com"),
    }

    # Firefox 不需要管理员权限，优先尝试
    browsers_to_try = [browser]
    for fb in ("firefox", "chrome", "edge", "brave", "opera"):
        if fb not in browsers_to_try:
            browsers_to_try.append(fb)

    cookie_jar = None
    for brw in browsers_to_try:
        extractor = _extractors.get(brw)
        if not extractor:
            continue
        try:
            cookie_jar = extractor()
            if cookie_jar:
                cookie_count = len(list(cookie_jar))
                if cookie_count > 0:
                    logger.info("从 %s 成功提取 %d 个 Reddit cookies", brw, cookie_count)
                    break
                else:
                    logger.info("%s 提取到 0 个 Reddit cookies，尝试下一个浏览器", brw)
                    cookie_jar = None
        except PermissionError as e:
            logger.warning("从 %s 提取 cookies 需要管理员权限或浏览器正在运行: %s", brw, e)
        except Exception as e:
            err_msg = str(e)
            if "admin" in err_msg.lower():
                logger.warning("从 %s 提取 cookies 需要管理员权限（请关闭浏览器后重试）", brw)
            else:
                logger.warning("从 %s 提取 Reddit cookies 失败: %s", brw, e)

    if not cookie_jar:
        return None

    cookies = {}
    for cookie in cookie_jar:
        if cookie.value:
            cookies[cookie.name] = cookie.value
    if not cookies:
        return None

    has_auth = any(k in cookies for k in ("reddit_session", "session", "token"))
    logger.info("Reddit 浏览器 cookies: 提取到 %d 个，认证 cookie: %s",
                len(cookies), "存在" if has_auth else "缺失")
    return cookies


def _reddit_cache_cookies(cookies: dict) -> bool:
    """将提取的 cookies 缓存到数据库。"""
    try:
        from database import set_setting, _protect_setting
        cookie_json = json.dumps(cookies)
        set_setting("reddit_cached_cookies", _protect_setting("reddit_cached_cookies", cookie_json))
        logger.info("Reddit: cookies 已缓存到数据库 (%d 个)", len(cookies))
        return True
    except Exception as e:
        logger.warning("Reddit: 缓存 cookies 失败: %s", e)
        return False


def _reddit_get_cached_cookies() -> dict | None:
    """从数据库读取缓存的 cookies。"""
    try:
        from database import get_setting
        cached = get_setting("reddit_cached_cookies", "")
        if not cached:
            return None
        cookies = json.loads(cached)
        if isinstance(cookies, dict) and cookies:
            return cookies
    except Exception as e:
        logger.warning("Reddit: 读取缓存 cookies 失败: %s", e)
    return None


def reddit_extract_and_cache_cookies(browser: str = "chrome") -> dict:
    """提取浏览器 cookies 并缓存。供 API 端点调用。

    返回 {"success": bool, "message": str, "cookie_count": int}
    """
    # 先重置 session
    _reddit_reset_cookie_session()

    cookies = _reddit_extract_browser_cookies(browser)
    if not cookies:
        return {
            "success": False,
            "message": (
                "无法提取 cookies。请确保：\n"
                "1. 已在浏览器中登录 Reddit (reddit.com)\n"
                "2. 已关闭 Chrome/Edge 浏览器（运行时会锁定 cookie 文件）\n"
                "3. 或以管理员身份运行本应用\n"
                "4. 或使用 Firefox（无需关闭）\n"
                "5. 或切换为手动模式配置 cookies"
            ),
            "cookie_count": 0,
        }

    # 缓存
    _reddit_cache_cookies(cookies)

    has_auth = any(k in cookies for k in ("reddit_session", "session", "token"))
    if not has_auth:
        return {
            "success": False,
            "message": (
                f"提取到 {len(cookies)} 个 cookies，但未找到认证 cookie。\n"
                "请确保已在浏览器中登录 Reddit 并完全关闭浏览器后重试。"
            ),
            "cookie_count": len(cookies),
        }

    return {
        "success": True,
        "message": f"成功提取并缓存 {len(cookies)} 个 Reddit cookies",
        "cookie_count": len(cookies),
    }


def _reddit_parse_manual_cookies(cookie_str: str) -> dict | None:
    """解析手动粘贴的 cookies 字符串。

    支持两种格式：
    1. JSON 数组: [{"name":"reddit_session","value":"xxx"},...]
    2. 分号分隔: reddit_session=xxx; token=yyy
    """
    if not cookie_str or not cookie_str.strip():
        return None

    cookie_str = cookie_str.strip()

    # JSON 数组格式
    if cookie_str.startswith("["):
        try:
            arr = json.loads(cookie_str)
            cookies = {}
            for item in arr:
                name = item.get("name", "")
                value = item.get("value", "")
                if name and value:
                    cookies[name] = value
            return cookies if cookies else None
        except (ValueError, TypeError) as e:
            logger.warning("Reddit cookies JSON 解析失败: %s", e)

    # 分号分隔格式
    cookies = {}
    for line in cookie_str.replace("\n", ";").split(";"):
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and v:
                cookies[k] = v
    return cookies if cookies else None


def _reddit_get_cookie_session():
    """获取带 cookies 的 requests.Session。

    优先级：手动配置 cookies > 缓存的浏览器 cookies > 浏览器实时提取
    """
    global _reddit_cookie_session, _reddit_cookie_session_ready

    if _reddit_cookie_session is not None and _reddit_cookie_session_ready:
        return _reddit_cookie_session

    cfg = _reddit_get_cookie_config()
    cookies = None

    # 1. 手动配置的 cookies
    if cfg["method"] == "manual" and cfg["cookies"]:
        cookies = _reddit_parse_manual_cookies(cfg["cookies"])
        if cookies:
            logger.info("Reddit: 使用手动配置的 cookies (%d 个)", len(cookies))

    # 2. 缓存的浏览器 cookies（最常用路径）
    if not cookies:
        cookies = _reddit_get_cached_cookies()
        if cookies:
            logger.info("Reddit: 使用缓存的浏览器 cookies (%d 个)", len(cookies))

    # 3. 尝试浏览器实时提取（Chrome 运行时可能失败）
    if not cookies and cfg["method"] in ("browser", "auto", ""):
        cookies = _reddit_extract_browser_cookies(cfg["browser"])
        if cookies:
            logger.info("Reddit: 从浏览器 %s 实时提取到 cookies (%d 个)", cfg["browser"], len(cookies))
            _reddit_cache_cookies(cookies)

    # 4. 手动 cookies 作为 fallback
    if not cookies and cfg["cookies"]:
        cookies = _reddit_parse_manual_cookies(cfg["cookies"])
        if cookies:
            logger.info("Reddit: 浏览器提取失败，回退到手动 cookies")

    if not cookies:
        logger.warning("Reddit: 无法获取任何 cookies，Cookie 认证不可用")
        return None

    import requests
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    })
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".reddit.com")

    # 预热
    try:
        logger.info("Reddit: 正在预热 Cookie 会话...")
        resp = session.get("https://www.reddit.com/", timeout=15)
        if resp.status_code == 200:
            _reddit_cookie_session_ready = True
            logger.info("Reddit: Cookie 会话预热成功 (cookies: %d)", len(session.cookies))
        else:
            logger.warning("Reddit: Cookie 会话预热返回 HTTP %s", resp.status_code)
            _reddit_cookie_session_ready = True
    except Exception as e:
        logger.warning("Reddit: Cookie 会话预热失败: %s", e)
        _reddit_cookie_session_ready = True

    _reddit_cookie_session = session
    return session


def _reddit_reset_cookie_session():
    """重置 Cookie 会话和 PRAW 实例，下次请求时重新初始化。"""
    global _reddit_cookie_session, _reddit_cookie_session_ready
    if _reddit_cookie_session is not None:
        try:
            _reddit_cookie_session.close()
        except Exception:
            pass
    _reddit_cookie_session = None
    _reddit_cookie_session_ready = False
    _reddit_reset_praw()


def _reddit_cookie_get(path: str, params=None, log_tag: str = ""):
    """使用 Cookie 认证请求 Reddit 内部 JSON API。

    返回 requests.Response 或 None。
    包含指数退避重试和 Gaussian jitter 反检测。
    """
    import random

    session = _reddit_get_cookie_session()
    if session is None:
        return None

    # 尝试 www.reddit.com 和 old.reddit.com
    bases = ["https://www.reddit.com", "https://old.reddit.com"]
    backoff_times = [5, 15, 30]  # 指数退避（保守，保护账号）

    for base in bases:
        url = f"{base}{path}"
        for attempt in range(3):
            try:
                # Gaussian jitter: 在请求间添加随机延迟（1-4s），模拟人类行为
                if attempt > 0:
                    jitter = random.gauss(2.0, 0.8)
                    jitter = max(1.0, min(jitter, 5.0))
                    time.sleep(jitter)

                # JSON API 请求头
                headers = dict(session.headers)
                headers["Accept"] = "application/json, text/plain, */*"
                headers["X-Requested-With"] = "XMLHttpRequest"

                resp = session.get(url, params=params, headers=headers, timeout=20)

                if resp.status_code == 200:
                    return resp

                if resp.status_code == 403:
                    logger.warning("Reddit Cookie 403 [%s] at %s — 停止重试，保护账号", log_tag, base)
                    # 403 可能是封号信号，立即停止不重试
                    break

                if resp.status_code == 429:
                    wait = backoff_times[min(attempt, len(backoff_times) - 1)]
                    # 加入 Gaussian jitter
                    wait += random.gauss(0, 1)
                    wait = max(3, min(wait, 40))
                    logger.warning("Reddit Cookie 429 限流 [%s]，等待 %.1fs (attempt %d/3)", log_tag, wait, attempt + 1)
                    time.sleep(wait)
                    continue

                logger.warning("Reddit Cookie HTTP %s [%s] at %s", resp.status_code, log_tag, base)
                break  # 其他状态码，换域名

            except Exception as e:
                logger.warning("Reddit Cookie 请求异常 [%s]: %s", log_tag, e)
                if attempt < 2:
                    time.sleep(backoff_times[attempt])
                    continue
                break

    return None


# 全局 Session 复用连接
_reddit_session = None
_reddit_session_ready = False


def _get_reddit_session():
    """获取复用的 requests.Session，并访问首页获取 cookies（Reddit 要求 cookies 才允许 RSS 访问）"""
    global _reddit_session, _reddit_session_ready
    if _reddit_session is None:
        import requests
        _reddit_session = requests.Session()
        # 使用 HTML Accept 头（先访问首页需要 HTML Accept）
        _reddit_session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
    # 首次使用或被重置时，访问首页获取 cookies
    if not _reddit_session_ready:
        try:
            logger.info("Reddit: 正在初始化会话（访问首页获取 cookies）...")
            resp = _reddit_session.get(f"{_REDDIT_BASE}/", timeout=15)
            if resp.status_code == 200:
                _reddit_session_ready = True
                logger.info(f"Reddit: 会话初始化成功，获取到 {len(_reddit_session.cookies)} 个 cookies")
            else:
                logger.warning(f"Reddit: 首页访问返回 {resp.status_code}")
        except Exception as e:
            logger.warning(f"Reddit: 首页访问失败: {e}")
    return _reddit_session


def _reset_reddit_session():
    """重置会话状态，下次请求时会重新访问首页"""
    global _reddit_session_ready
    _reddit_session_ready = False


def _reddit_rss_get(path: str, params: dict | None = None, log_tag: str = "") -> str | None:
    """
    Reddit RSS GET 请求。
    - 首次使用时自动访问首页获取 cookies（Reddit 无 cookies 时返回 403）
    - 403 时重置会话并重试一次
    - 429 时按指数退避等待重试（10s → 30s → 60s），并尊重 Retry-After 头
    """
    session = _get_reddit_session()
    url = f"{_REDDIT_BASE}{path}"
    backoff_times = [10, 30, 60]  # 指数退避等待秒数
    for attempt in range(4):  # 最多 4 次：原始 + 403重置 + 2次429退避
        try:
            resp = session.get(url, params=params, timeout=20)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 403:
                # cookies 可能过期，重置会话并重试
                if attempt == 0:
                    logger.warning(f"Reddit 403 [{log_tag}]，重置会话重试...")
                    _reset_reddit_session()
                    session = _get_reddit_session()
                    continue
                else:
                    logger.warning(f"Reddit 403 [{log_tag}]，重试仍失败")
                    return None
            if resp.status_code == 429:
                # 优先使用 Reddit 返回的 Retry-After 头
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = int(retry_after)
                    except ValueError:
                        wait = backoff_times[min(attempt, len(backoff_times) - 1)]
                else:
                    wait = backoff_times[min(attempt, len(backoff_times) - 1)]
                if attempt < 3:
                    logger.warning(f"Reddit 429 限流 [{log_tag}]，等待 {wait}s 后重试 (尝试 {attempt+1}/4)...")
                    time.sleep(wait)
                    continue
                else:
                    logger.warning(f"Reddit 429 限流 [{log_tag}]，重试上限，放弃")
                    return None
            logger.warning(f"Reddit HTTP {resp.status_code} [{log_tag}]")
            return None
        except Exception as e:
            logger.warning(f"Reddit 请求异常 [{log_tag}]: {e}")
            if attempt < 3:
                time.sleep(5)
                continue
            return None
    return None


def _parse_atom_entries(xml_text: str) -> list[dict]:
    """解析 Reddit Atom RSS feed，返回 entry 字典列表"""
    root = ET.fromstring(xml_text)
    entries_el = root.findall("atom:entry", _ATOM_NS)
    results = []
    for entry in entries_el:
        title_el = entry.find("atom:title", _ATOM_NS)
        link_el = entry.find("atom:link", _ATOM_NS)
        content_el = entry.find("atom:content", _ATOM_NS)
        updated_el = entry.find("atom:updated", _ATOM_NS)
        author_el = entry.find("atom:author/atom:name", _ATOM_NS)
        id_el = entry.find("atom:id", _ATOM_NS)

        title = title_el.text if title_el is not None else ""
        href = link_el.get("href", "") if link_el is not None else ""
        content_html = content_el.text if content_el is not None and content_el.text else ""
        content_text = re.sub(r"<[^>]+>", "", content_html).strip()
        content_text = content_text.replace("&#39;", "'").replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        author = author_el.text if author_el is not None else ""
        if author.startswith("/u/"):
            author = author[3:]
        updated = updated_el.text if updated_el is not None else ""

        post_id = ""
        if "/comments/" in href:
            post_id = href.split("/comments/")[-1].split("/")[0]

        subreddit = ""
        m = re.search(r"/r/([^/]+)", href)
        if m:
            subreddit = m.group(1)

        published = ""
        if updated:
            try:
                published = updated[:10].replace("-", "")
            except Exception:
                pass

        results.append({
            "title": title,
            "url": href,
            "post_id": post_id,
            "subreddit": subreddit,
            "author": author,
            "content": content_text,
            "published": published,
            "id_str": id_el.text if id_el is not None else "",
        })
    return results


def search_posts_reddit(keyword: str, limit: int = 10) -> list[dict]:
    """
    搜索 Reddit 帖子。通过 RSS/Atom feed 搜索。
    支持逗号分隔的多关键词：会拆分后逐个搜索，合并去重结果。
    """
    # 拆分逗号分隔的关键词，过滤空值
    keywords = [k.strip() for k in keyword.split(",") if k.strip()]
    if not keywords:
        keywords = [keyword]

    all_posts = []
    seen_ids = set()

    for kw in keywords:
        params = {"q": kw, "limit": limit, "sort": "relevance"}
        resp = _reddit_rss_get("/search.rss", params=params, log_tag=f"search:{kw[:30]}")
        if resp is None:
            continue

        try:
            entries = _parse_atom_entries(resp)
            for entry in entries:
                post_id = entry["post_id"]
                if not post_id:
                    # 搜索结果第一条有时是 subreddit 页面（无 post_id），跳过
                    continue
                if post_id in seen_ids:
                    continue
                seen_ids.add(post_id)
                all_posts.append({
                    "video_id": post_id,
                    "title": entry["title"],
                    "channel": f"r/{entry['subreddit']}" if entry["subreddit"] else "",
                    "view_count": 0,
                    "comment_count": 0,
                    "published_at": entry["published"],
                    # 保存 subreddit 和 URL 供后续评论抓取使用
                    "_subreddit": entry["subreddit"],
                    "_url": entry["url"],
                    "source_url": entry["url"],
                })
        except Exception as e:
            logger.error(f"Reddit 搜索结果解析失败: {kw} - {e}")

    # 限制总数
    return all_posts[:limit]


def extract_comments_reddit(post_id: str, max_comments: int = 500, subreddit: str = "", post_url: str = "") -> list[dict]:
    """
    提取 Reddit 帖子评论。通过 RSS/Atom feed 获取。
    post_url: 可选的完整帖子 URL，用于构建更精确的评论 RSS 地址。
    """
    # 构建评论 RSS 路径，优先用完整 URL（含 title slug），其次用 subreddit+id
    path = None
    if post_url and "/comments/" in post_url:
        # 从完整 URL 提取路径部分: /r/sub/comments/id/title/
        m = re.search(r"(/r/\S+/comments/\S+)", post_url)
        if m:
            path = m.group(1).rstrip("/") + "/.rss"
    if path is None:
        if subreddit:
            path = f"/r/{subreddit}/comments/{post_id}/.rss"
        else:
            path = f"/comments/{post_id}/.rss"

    resp = _reddit_rss_get(path, log_tag=f"comments:{post_id}")
    if resp is None:
        return []

    comments = []
    try:
        entries = _parse_atom_entries(resp)
        # 第一条 entry 通常是帖子本身，后续才是评论
        for i, entry in enumerate(entries):
            if i == 0:
                # 帖子本身，跳过
                continue
            body = entry["content"].strip()
            if not body or body == "[deleted]" or body == "[removed]":
                continue
            comments.append({
                # RSS 的 atom:id 包含评论层级，必须优先使用它做去重；
                # 不能使用 post_id，否则同一帖子的所有评论会被压成一条。
                "original_id": entry.get("id_str") or entry.get("post_id") or str(uuid.uuid4()),
                "content": body,
                "author": entry.get("author", ""),
                "like_count": 0,
                "posted_at": entry.get("published", ""),
                "source_url": entry.get("url", ""),
            })
            if len(comments) >= max_comments:
                break
    except ET.ParseError as e:
        logger.error(f"Reddit 评论 RSS 解析失败: {post_id} - {e}")
    except Exception as e:
        logger.error(f"Reddit 评论提取异常: {post_id} - {e}")

    logger.info(f"Reddit 评论提取: {post_id} → {len(comments)} 条评论")
    return comments


# ---------------------------------------------------------------------------
# Reddit compatibility layer (v0.7 regression fix)
#
# The previous release used Reddit's JSON endpoints successfully.  Keep that
# path as the primary implementation and use RSS only as a fallback.  The
# original RSS functions above are intentionally left in place for backwards
# compatibility, but the names are rebound below before the platform registry
# is constructed.

_reddit_session_v2 = None
_reddit_session_warmed = False


def _reddit_session_v2_get():
    """获取复用的 requests.Session，首次使用时自动访问首页获取 cookies。

    Reddit 要求 cookies 才允许匿名 JSON/RSS 访问，否则返回 403。
    所有路径（JSON / RSS / HTML）都通过此函数获取 session，
    确保首次请求前自动完成预热。
    """
    global _reddit_session_v2, _reddit_session_warmed
    if _reddit_session_v2 is None:
        import requests
        _reddit_session_v2 = requests.Session()
        _reddit_session_v2.headers.update(_HEADERS)
    if not _reddit_session_warmed:
        try:
            logger.info("Reddit: 正在预热会话（访问首页获取 cookies）...")
            _reddit_session_v2.get(
                "https://www.reddit.com/",
                headers={
                    "User-Agent": _HEADERS["User-Agent"],
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=10,
            )
            _reddit_session_warmed = True
            logger.info("Reddit: 会话预热完成，获取 %d 个 cookies", len(_reddit_session_v2.cookies))
        except Exception as e:
            logger.warning("Reddit: 会话预热失败: %s", e)
    return _reddit_session_v2


def _reddit_session_v2_reset():
    """重置 session 和预热状态，下次请求时会重新预热。"""
    global _reddit_session_v2, _reddit_session_warmed
    if _reddit_session_v2 is not None:
        try:
            _reddit_session_v2.close()
        except Exception:
            pass
    _reddit_session_v2 = None
    _reddit_session_warmed = False


def _reddit_http_get_v2(path: str, params=None, mode: str = "json", log_tag: str = ""):
    """GET Reddit via www and old domains, raising instead of returning [] on failure."""
    session = _reddit_session_v2_get()
    headers = dict(_HEADERS if mode == "json" else _REDDIT_HEADERS)
    errors = []
    for base in _REDDIT_BASES:
        for attempt in range(2):
            try:
                response = session.get(
                    f"{base}{path}", params=params, headers=headers, timeout=15
                )
                if response.status_code == 200:
                    return response
                if response.status_code == 429 and attempt == 0:
                    retry_after = response.headers.get("Retry-After", "1")
                    try:
                        wait = max(1, min(int(float(retry_after)), 5))
                    except (TypeError, ValueError):
                        wait = 1
                    logger.warning("Reddit 429 [%s], retrying in %ss", log_tag, wait)
                    time.sleep(wait)
                    continue
                errors.append(f"{base}: HTTP {response.status_code}")
                break
            except Exception as exc:
                errors.append(f"{base}: {type(exc).__name__}: {exc}")
                break
    raise RedditFetchError(log_tag or path, errors)



# ==================== Reddit HTML Scraping (no OAuth required) ====================

_REDDIT_HTML_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def _reddit_warmup_session():
    """预热已集成到 _reddit_session_v2_get() 中，此函数保留兼容性。"""
    _reddit_session_v2_get()


def _reddit_html_search(keyword: str, limit: int) -> list[dict]:
    """Search Reddit via old.reddit.com HTML (no OAuth needed)."""
    _reddit_warmup_session()
    session = _reddit_session_v2_get()
    time.sleep(_REDDIT_DELAY)
    response = session.get(
        "https://old.reddit.com/search/",
        params={
            "q": keyword,
            "sort": "relevance",
            "t": "year",
            "limit": min(limit * 2, 100),
        },
        headers=_REDDIT_HTML_HEADERS,
        timeout=15,
    )
    if response.status_code != 200:
        raise RedditFetchError(f"html-search:{keyword[:30]}", f"HTTP {response.status_code}")
    posts = _reddit_parse_old_search_html(response.text, limit)
    if not posts:
        raise RedditFetchError(f"html-search:{keyword[:30]}", "no results parsed from HTML")
    return posts


def _reddit_parse_old_search_html(html_text: str, limit: int) -> list[dict]:
    """Parse search results from old.reddit.com HTML."""
    posts = []
    seen = set()
    blocks = re.split(r'class="search-result\b', html_text)
    for block in blocks[1:]:
        title_match = re.search(
            r'<a\s+class="search-title[^"]*"\s+href="([^"]+)"[^>]*>(.*?)</a>',
            block, re.DOTALL,
        )
        if not title_match:
            continue
        raw_url = html.unescape(title_match.group(1))
        title = html.unescape(re.sub(r"<[^>]+>", "", title_match.group(2))).strip()

        if "/comments/" not in raw_url:
            continue

        if raw_url.startswith("/"):
            raw_url = "https://www.reddit.com" + raw_url

        post_id = raw_url.split("/comments/")[-1].split("/")[0].split("?")[0].split("#")[0]
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)

        sub_match = re.search(r"/r/([^/]+)", raw_url)
        subreddit = sub_match.group(1) if sub_match else ""

        score = 0
        score_match = re.search(r"([\d,]+)\s*points?", block)
        if score_match:
            try:
                score = int(score_match.group(1).replace(",", ""))
            except ValueError:
                pass

        comment_count = 0
        comment_match = re.search(r"([\d,]+)\s*comments?", block)
        if comment_match:
            try:
                comment_count = int(comment_match.group(1).replace(",", ""))
            except ValueError:
                pass

        published = ""
        time_match = re.search(r'datetime="([^"]+)"', block)
        if time_match:
            published = time_match.group(1)[:10].replace("-", "")

        selftext = ""
        expando_match = re.search(
            r'class="search-expando[^"]*"[^>]*>(.*?)(?:</div>\s*</div>|</div>\s*<div)',
            block, re.DOTALL,
        )
        if expando_match:
            selftext = html.unescape(re.sub(r"<[^>]+>", " ", expando_match.group(1))).strip()[:500]

        posts.append({
            "video_id": post_id,
            "title": title,
            "content": selftext,
            "channel": f"r/{subreddit}" if subreddit else "",
            "view_count": score,
            "comment_count": comment_count,
            "published_at": published,
            "_subreddit": subreddit,
            "_url": raw_url,
            "source_url": raw_url,
        })
        if len(posts) >= limit:
            break
    return posts


def _reddit_html_comments(post_url: str, subreddit: str, post_id: str, max_comments: int) -> list[dict]:
    """Fetch comments from old.reddit.com HTML (no OAuth needed)."""
    _reddit_warmup_session()
    session = _reddit_session_v2_get()
    time.sleep(_REDDIT_DELAY)

    url = None
    if post_url:
        url = post_url
        if url.startswith("/"):
            url = "https://old.reddit.com" + url
        url = url.replace("https://www.reddit.com", "https://old.reddit.com")
        if "?" in url:
            url = url.split("?")[0]
        if not url.endswith("/"):
            url += "/"
        url += "?limit=500"
    elif subreddit:
        url = f"https://old.reddit.com/r/{subreddit}/comments/{post_id}/?limit=500"
    else:
        url = f"https://old.reddit.com/comments/{post_id}/?limit=500"

    response = session.get(url, headers=_REDDIT_HTML_HEADERS, timeout=15)
    if response.status_code != 200:
        raise RedditFetchError(f"html-comments:{post_id}", f"HTTP {response.status_code}")
    return _reddit_parse_old_comments_html(response.text, max_comments)


def _reddit_parse_old_comments_html(html_text: str, max_comments: int) -> list[dict]:
    """Parse comments from old.reddit.com comment page HTML."""
    comments = []
    blocks = re.split(r'<div class="comment\b', html_text)
    for block in blocks[1:]:
        md_match = re.search(r'<div class="md[^"]*">(.*?)</div>', block, re.DOTALL)
        if not md_match:
            continue
        body = html.unescape(re.sub(r"<[^>]+>", " ", md_match.group(1))).strip()
        body = re.sub(r"\s+", " ", body)
        if not body or body in {"[deleted]", "[removed]"}:
            continue

        author = ""
        author_match = re.search(r'<a\s+class="author[^"]*"[^>]*>([^<]+)</a>', block)
        if author_match:
            author = html.unescape(author_match.group(1).strip())

        score = 0
        score_match = re.search(r'class="score\s+[^"]*"[^>]*>\s*(\d+)\s*points?', block)
        if not score_match:
            score_match = re.search(r'(\d+)\s*points?', block)
        if score_match:
            try:
                score = int(score_match.group(1))
            except ValueError:
                pass

        comments.append({
            "original_id": str(uuid.uuid4()),
            "content": body,
            "author": author,
            "like_count": score,
            "posted_at": "",
            "source_url": "",
        })
        if len(comments) >= max_comments:
            break
    return comments



def _reddit_date_v2(value) -> str:
    try:
        return time.strftime("%Y%m%d", time.gmtime(float(value))) if value else ""
    except (TypeError, ValueError, OverflowError):
        return str(value or "")[:10].replace("-", "")


def _reddit_post_from_json_v2(data: dict) -> dict:
    post_id = str(data.get("id") or "")
    permalink = data.get("permalink") or ""
    source_url = (
        f"https://www.reddit.com{permalink}" if permalink.startswith("/")
        else data.get("url", "")
    )
    return {
        "video_id": post_id,
        "title": data.get("title", ""),
        "content": data.get("selftext", "") or "",
        "channel": f"r/{data.get('subreddit', '')}" if data.get("subreddit") else "",
        "view_count": data.get("score", 0) or 0,
        "comment_count": data.get("num_comments", 0) or 0,
        "published_at": _reddit_date_v2(data.get("created_utc")),
        "_subreddit": data.get("subreddit", "") or "",
        "_url": source_url,
        "source_url": source_url,
    }


def _reddit_parse_atom_v2(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    result = []
    entries = root.findall("atom:entry", _ATOM_NS)
    if entries:
        for entry in entries:
            title = entry.findtext("atom:title", "", _ATOM_NS)
            link = entry.find("atom:link", _ATOM_NS)
            href = link.get("href", "") if link is not None else ""
            content = entry.findtext("atom:content", "", _ATOM_NS) or ""
            updated = entry.findtext("atom:updated", "", _ATOM_NS) or ""
            author = entry.findtext("atom:author/atom:name", "", _ATOM_NS) or ""
            atom_id = entry.findtext("atom:id", "", _ATOM_NS) or ""
            content = html.unescape(re.sub(r"<[^>]+>", "", content)).strip()
            author = author[3:] if author.startswith("/u/") else author
            post_id = href.split("/comments/")[-1].split("/")[0] if "/comments/" in href else ""
            match = re.search(r"/r/([^/]+)", href)
            result.append({
                "title": html.unescape(title or ""), "url": href, "post_id": post_id,
                "subreddit": match.group(1) if match else "", "author": author,
                "content": content, "published": _reddit_date_v2(updated), "id_str": atom_id,
            })
        return result
    for item in root.findall(".//item"):
        title = item.findtext("title", "") or ""
        href = item.findtext("link", "") or ""
        content = item.findtext("description", "") or ""
        post_id = href.split("/comments/")[-1].split("/")[0] if "/comments/" in href else ""
        match = re.search(r"/r/([^/]+)", href)
        result.append({
            "title": html.unescape(title), "url": href, "post_id": post_id,
            "subreddit": match.group(1) if match else "", "author": "",
            "content": html.unescape(re.sub(r"<[^>]+>", "", content)).strip(),
            "published": _reddit_date_v2(item.findtext("pubDate", "")),
            "id_str": item.findtext("guid", "") or "",
        })
    return result


def _reddit_posts_from_rss_v2(xml_text: str) -> list[dict]:
    posts = []
    for entry in _reddit_parse_atom_v2(xml_text):
        if entry.get("post_id"):
            posts.append({
                "video_id": entry["post_id"], "title": entry.get("title", ""),
                "channel": f"r/{entry['subreddit']}" if entry.get("subreddit") else "",
                "view_count": 0, "comment_count": 0,
                "published_at": entry.get("published", ""),
                "_subreddit": entry.get("subreddit", ""), "_url": entry.get("url", ""),
                "source_url": entry.get("url", ""),
                "content": entry.get("content", ""),
            })
    return posts


def _reddit_search_json_v2(keyword: str, limit: int) -> list[dict]:
    # --- Cookie 认证路径（方案B：最高优先级）---
    if _has_reddit_cookie_auth():
        resp = _reddit_cookie_get(
            "/search.json",
            params={"q": keyword, "limit": limit, "sort": "relevance", "type": "link", "raw_json": 1},
            log_tag=f"cookie-search:{keyword[:30]}",
        )
        if resp is not None:
            try:
                payload = resp.json()
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    children = (payload.get("data") or {}).get("children") or []
                    logger.info("Reddit Cookie search '%s' -> %d results", keyword[:30], len(children))
                    return [
                        _reddit_post_from_json_v2(child.get("data", {}))
                        for child in children
                        if child.get("data", {}).get("id")
                    ]
            except (ValueError, TypeError) as exc:
                logger.warning("Reddit Cookie search parse error: %s", exc)
        else:
            logger.warning("Reddit Cookie search failed, falling back to OAuth/anonymous")

    # --- OAuth primary path ---
    if _has_reddit_oauth():
        resp = _reddit_oauth_get(
            "/search",
            params={"q": keyword, "limit": limit, "sort": "relevance", "type": "link", "raw_json": 1},
            log_tag=f"oauth-search:{keyword[:30]}",
        )
        if resp is not None:
            try:
                payload = resp.json()
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    children = (payload.get("data") or {}).get("children") or []
                    logger.info("Reddit OAuth search '%s' -> %d results", keyword[:30], len(children))
                    return [
                        _reddit_post_from_json_v2(child.get("data", {}))
                        for child in children
                        if child.get("data", {}).get("id")
                    ]
            except (ValueError, TypeError) as exc:
                logger.warning("Reddit OAuth search parse error: %s", exc)
        else:
            logger.warning("Reddit OAuth search failed")

    # --- 认证路径全部失败：快速失败，不落入匿名分支 ---
    if _has_reddit_oauth() or _has_reddit_cookie_auth():
        raise RedditFetchError(
            f"search:{keyword[:30]}",
            "Cookie/OAuth 认证搜索均失败（跳过匿名路径：Reddit 已封堵匿名访问）",
        )

    # --- Anonymous .json fallback（仅未配置认证时）---
    response = _reddit_http_get_v2(
        "/search.json",
        params={"q": keyword, "limit": limit, "sort": "relevance", "type": "link", "raw_json": 1},
        mode="json", log_tag=f"search.json:{keyword[:30]}",
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RedditFetchError("search.json parse", str(exc)) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise RedditFetchError("search.json response", "响应缺少 data 字段")
    children = (payload.get("data") or {}).get("children") or []
    return [
        _reddit_post_from_json_v2(child.get("data", {}))
        for child in children
        if child.get("data", {}).get("id")
    ]


def search_posts_reddit_v2(keyword: str, limit: int = 10) -> list[dict]:
    keywords = [k.strip() for k in keyword.split(",") if k.strip()] or [keyword]
    posts, seen, errors = [], set(), []
    successful = 0
    for kw in keywords:
        found = []
        # --- PRAW 路径（最高优先级，官方推荐）---
        if _has_reddit_oauth():
            try:
                found = _praw_search_posts(kw, limit)
                successful += 1
            except RedditFetchError as praw_exc:
                errors.append(str(praw_exc))
                found = []

        # PRAW 失败或未配置，尝试 Cookie/OAuth JSON 路径
        if not found:
            try:
                found = _reddit_search_json_v2(kw, limit)
                successful += 1
            except RedditFetchError as json_exc:
                errors.append(str(json_exc))
                # 认证已配置时 _reddit_search_json_v2 已快速失败，
                # 匿名 RSS/HTML 对封堵后的 Reddit 只是浪费时间，直接跳过
                if _has_reddit_oauth() or _has_reddit_cookie_auth():
                    found = []
                else:
                    # JSON 失败后，使用 _reddit_rss_get（长退避策略）搜索 RSS
                    rss_text = _reddit_rss_get(
                        "/search.rss",
                        params={"q": kw, "limit": limit, "sort": "relevance"},
                        log_tag=f"search.rss:{kw[:30]}",
                    )
                    if rss_text is not None:
                        try:
                            found = _reddit_posts_from_rss_v2(rss_text)
                            successful += 1
                            logger.warning("Reddit JSON unavailable; RSS fallback used for %s", kw)
                        except ET.ParseError as rss_exc:
                            errors.append(str(rss_exc))
                            found = []
                    else:
                        # RSS 也失败，尝试 HTML
                        try:
                            found = _reddit_html_search(kw, limit)
                            successful += 1
                            logger.info("Reddit HTML search for '%s' -> %d results", kw[:30], len(found))
                        except RedditFetchError as html_exc:
                            errors.append(str(html_exc))
                            found = []
        for post in found:
            post_id = post.get("video_id")
            if post_id and post_id not in seen:
                seen.add(post_id)
                posts.append(post)
    if successful == 0:
        hint = _reddit_block_hint(errors)
        if hint:
            errors.append(hint)
        raise RedditFetchError("search", errors)
    if errors:
        logger.warning("Reddit search fallback/errors: %s", "; ".join(errors[:3]))
    return posts[:limit]


def _reddit_iter_comments_v2(children):
    for child in children or []:
        if child.get("kind") != "t1":
            continue
        data = child.get("data", {}) or {}
        yield data
        replies = data.get("replies")
        if isinstance(replies, dict):
            yield from _reddit_iter_comments_v2(replies.get("data", {}).get("children", []))


def _reddit_comments_json_v2(payload, max_comments: int) -> list[dict]:
    if not isinstance(payload, list) or len(payload) < 2:
        raise ValueError("Reddit comments response is not a JSON list")
    comments = []
    for item in _reddit_iter_comments_v2(payload[1].get("data", {}).get("children", [])):
        body = (item.get("body") or "").strip()
        if not body or body in {"[deleted]", "[removed]"}:
            continue
        permalink = item.get("permalink") or ""
        source_url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else ""
        parent_full = item.get("parent_id") or ""
        parent_id = parent_full[3:] if parent_full.startswith("t1_") else None
        try:
            depth = int(item.get("depth") or 0)
        except (TypeError, ValueError):
            depth = 0
        comments.append({
            "original_id": str(item.get("id") or uuid.uuid4()), "content": body,
            "author": item.get("author") or "", "like_count": item.get("score", 0) or 0,
            "posted_at": _reddit_date_v2(item.get("created_utc")), "source_url": source_url,
            "parent_id": parent_id, "depth": depth,
        })
        if len(comments) >= max_comments:
            break
    return comments


def extract_comments_reddit_v2(post_id: str, max_comments: int = 500, subreddit: str = "", post_url: str = "") -> list[dict]:
    errors = []
    # --- PRAW 路径（最高优先级，官方推荐）---
    if _has_reddit_oauth():
        try:
            comments = _praw_extract_comments(post_id, max_comments)
            if comments:
                return comments
        except RedditFetchError as praw_exc:
            errors.append(str(praw_exc))
            logger.warning("PRAW comments failed, falling back to Cookie/OAuth: %s", praw_exc)

    # --- Cookie 认证路径（方案B）---
    if _has_reddit_cookie_auth():
        resp = _reddit_cookie_get(
            f"/comments/{post_id}.json",
            params={"limit": max_comments, "raw_json": 1},
            log_tag=f"cookie-comments:{post_id}",
        )
        if resp is not None:
            try:
                comments = _reddit_comments_json_v2(resp.json(), max_comments)
                logger.info("Reddit Cookie comments: %s -> %s", post_id, len(comments))
                return comments
            except (ValueError, TypeError) as exc:
                logger.warning("Reddit Cookie comments parse error: %s", exc)
        else:
            logger.warning("Reddit Cookie comments failed, falling back to OAuth/anonymous")

    # --- OAuth primary path ---
    if _has_reddit_oauth():
        resp = _reddit_oauth_get(
            f"/comments/{post_id}",
            params={"limit": max_comments, "raw_json": 1},
            log_tag=f"oauth-comments:{post_id}",
        )
        if resp is not None:
            try:
                comments = _reddit_comments_json_v2(resp.json(), max_comments)
                logger.info("Reddit OAuth comments: %s -> %s", post_id, len(comments))
                return comments
            except (ValueError, TypeError) as exc:
                logger.warning("Reddit OAuth comments parse error: %s", exc)
        else:
            logger.warning("Reddit OAuth comments failed")

    # --- 认证路径全部失败：快速失败 ---
    # Reddit 2026-05 起封堵匿名 .json/RSS/HTML 访问，配置了认证的请求
    # 掉进匿名链路只会白白消耗分钟级退避时间。
    if _has_reddit_oauth() or _has_reddit_cookie_auth():
        errors.append("PRAW/Cookie/OAuth 认证路径均失败（跳过匿名路径：Reddit 已封堵匿名访问）")
        hint = _reddit_block_hint(errors)
        if hint:
            errors.append(hint)
        raise RedditFetchError(f"comments {post_id}", errors)

    # --- 以下匿名路径仅在完全未配置认证时尽力一试 ---
    # --- JSON 路径（IP 被封时快速失败）---
    try:
        response = _reddit_http_get_v2(
            f"/comments/{post_id}.json", params={"limit": max_comments, "raw_json": 1},
            mode="json", log_tag=f"comments.json:{post_id}"
        )
        comments = _reddit_comments_json_v2(response.json(), max_comments)
        logger.info("Reddit comments (JSON): %s -> %s", post_id, len(comments))
        return comments
    except (RedditFetchError, ValueError, TypeError) as exc:
        errors.append(str(exc))

    # --- RSS 路径（使用 _reddit_rss_get 的长退避策略：10s→30s→60s）---
    # 这是 IP 被封后唯一可靠的评论获取路径
    path = None
    if post_url and "/comments/" in post_url:
        match = re.search(r"(/r/[^/]+/comments/[^/?#]+(?:/[^/?#]+)?)", post_url)
        if match:
            path = match.group(1).rstrip("/") + "/.rss"
    if path is None:
        path = f"/r/{subreddit}/comments/{post_id}/.rss" if subreddit else f"/comments/{post_id}/.rss"

    rss_text = _reddit_rss_get(path, log_tag=f"comments.rss:{post_id}")
    if rss_text is not None:
        try:
            comments = []
            for index, entry in enumerate(_reddit_parse_atom_v2(rss_text)):
                if index == 0:
                    continue
                body = (entry.get("content") or "").strip()
                if not body or body in {"[deleted]", "[removed]"}:
                    continue
                comments.append({
                    "original_id": entry.get("id_str") or str(uuid.uuid4()), "content": body,
                    "author": entry.get("author", ""), "like_count": 0,
                    "posted_at": entry.get("published", ""), "source_url": entry.get("url", ""),
                })
                if len(comments) >= max_comments:
                    break
            logger.info("Reddit comments (RSS): %s -> %s", post_id, len(comments))
            return comments
        except ET.ParseError as exc:
            errors.append(f"RSS parse: {exc}")

    # --- HTML 路径（最后手段）---
    try:
        comments = _reddit_html_comments(post_url, subreddit, post_id, max_comments)
        logger.info("Reddit HTML comments: %s -> %s", post_id, len(comments))
        return comments
    except RedditFetchError as html_exc:
        errors.append(str(html_exc))

    hint = _reddit_block_hint(errors)
    if hint:
        errors.append(hint)
    raise RedditFetchError(f"comments {post_id}", errors)


# Rebind the public names used by PLATFORM_REGISTRY below.
search_posts_reddit = search_posts_reddit_v2
extract_comments_reddit = extract_comments_reddit_v2


# ==================== Instagram (Instaloader) ====================

_instaloader_instance = None
_instaloader_username = None


def _get_instagram_loader():
    """获取已登录的 Instaloader 实例。

    流程：
    1. 从数据库读取 Instagram 账号配置
    2. 尝试加载已保存的 session 文件
    3. 如果没有 session，用账号密码登录并保存
    4. 复用实例避免重复登录

    未配置账号时返回 None（匿名模式，功能受限）。
    """
    global _instaloader_instance, _instaloader_username
    if _instaloader_instance is not None:
        return _instaloader_instance

    import instaloader

    L = instaloader.Instaloader(
        download_pictures=False, download_videos=False,
        download_video_thumbnails=False, save_metadata=False,
        post_metadata_txt_pattern="", quiet=True,
    )

    # 从数据库读取 Instagram 账号配置
    try:
        from database import get_setting
        ig_user = get_setting("instagram_username", "")
        ig_password = get_setting("instagram_password", "")
    except Exception:
        ig_user = ""
        ig_password = ""

    if ig_user and ig_password:
        # 尝试加载已保存的 session
        session_dir = settings.DATA_DIR / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / f"instaloader_{ig_user}.session"

        loaded = False
        if session_file.exists():
            try:
                L.load_session_from_file(ig_user, str(session_file))
                logger.info("Instagram: 已加载保存的 session (%s)", ig_user)
                loaded = True
            except Exception as e:
                logger.warning("Instagram: 加载 session 失败: %s，将重新登录", e)

        if not loaded:
            try:
                logger.info("Instagram: 正在登录 (%s)...", ig_user)
                L.login(ig_user, ig_password)
                L.save_session_to_file(str(session_file))
                logger.info("Instagram: 登录成功，session 已保存")
            except Exception as e:
                logger.error("Instagram: 登录失败: %s — 将以匿名模式运行", e)
                # 登录失败，返回匿名实例
                _instaloader_instance = L
                _instaloader_username = None
                return L
        _instaloader_username = ig_user
    else:
        logger.info("Instagram: 未配置账号，以匿名模式运行（评论抓取可能受限）")

    _instaloader_instance = L
    return L


def search_posts_instagram(keyword: str, limit: int = 10) -> list[dict]:
    """
    Instagram 搜索 — 通过 Instaloader 按 hashtag 搜索帖子。
    必须配置账号（用户名+密码）才能使用，Instagram 已封锁匿名访问。
    """
    tag = re.sub(r"[^a-zA-Z0-9]", "", keyword).lower()
    if not tag:
        return []
    if _instaloader_username is None:
        raise RuntimeError(
            "Instagram 需要登录才能搜索。请在「设置 → Instagram 账号配置」中填入用户名和密码。"
            "（Instagram 已封锁匿名访问，所有 API 请求都返回 403 login_required）"
        )
    posts = []
    try:
        import instaloader
        L = _get_instagram_loader()
        hashtag = instaloader.Hashtag.from_name(L.context, tag)
        count = 0
        for post in hashtag.get_posts_resumable():
            if count >= limit:
                break
            posts.append({
                "video_id": post.shortcode,
                "title": f"#{tag} post",
                "channel": "",
                "view_count": post.likes,
                "comment_count": post.comments,
                "published_at": post.date.strftime("%Y%m%d") if post.date else "",
                "source_url": f"https://www.instagram.com/p/{post.shortcode}/",
            })
            count += 1
    except Exception as e:
        err_msg = str(e)
        if "login_required" in err_msg or "403" in err_msg:
            raise RuntimeError(
                f"Instagram 登录已过期或无效，请重新配置账号。错误: {err_msg}"
            ) from e
        logger.error(f"Instagram 搜索失败: {keyword} - {e}")
        raise RuntimeError(f"Instagram 搜索失败: {e}") from e
    return posts


def extract_comments_instagram(shortcode: str, max_comments: int = 500) -> list[dict]:
    """
    提取 Instagram 帖子评论 — 使用 Instaloader。
    需要登录才能抓取评论；未登录时抛出异常。
    """
    if _instaloader_username is None:
        raise RuntimeError(
            "Instagram 需要登录才能抓取评论。请在「设置 → Instagram 账号配置」中填入用户名和密码。"
        )

    comments = []
    try:
        import instaloader
        L = _get_instagram_loader()
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        count = 0
        for comment in post.get_comments():
            if count >= max_comments:
                break
            text = (comment.text or "").strip()
            if not text:
                continue
            author = ""
            try:
                author = comment.owner.username
            except Exception:
                pass
            comments.append({
                "original_id": str(getattr(comment, "id", uuid.uuid4())),
                "content": text,
                "author": author,
                "like_count": getattr(comment, "likes", 0) or 0,
                "posted_at": str(getattr(comment, "created_at", "")),
                "source_url": f"https://www.instagram.com/p/{shortcode}/",
            })
            count += 1
    except Exception as e:
        logger.error(f"Instagram 评论提取失败: {shortcode} - {e}")
        raise RuntimeError(f"Instagram 评论提取失败: {e}") from e
    return comments


# ==================== TikTok ====================

def search_videos_tiktok(keyword: str, limit: int = 10) -> list[dict]:
    """
    TikTok 搜索 — 通过 hashtag 页面
    注意: TikTok 反爬严格，可能需要 cookies
    """
    tag = re.sub(r"[^a-zA-Z0-9]", "", keyword).lower()
    if not tag:
        return []
    opts = {
        "quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True,
    }
    videos = []
    try:
        with YoutubeDL(opts) as ydl:
            result = ydl.extract_info(
                f"https://www.tiktok.com/tag/{tag}", download=False
            )
            entries = result.get("entries", []) if result else []
            for entry in entries[:limit]:
                vid = entry.get("id", "")
                if not vid:
                    continue
                videos.append({
                    "video_id": vid,
                    "title": entry.get("title", f"#{tag}"),
                    "channel": entry.get("channel", entry.get("uploader", "")),
                    "view_count": entry.get("view_count", 0) or 0,
                    "comment_count": entry.get("comment_count", 0) or 0,
                    "published_at": entry.get("upload_date", ""),
                    "source_url": f"https://www.tiktok.com/video/{vid}",
                })
    except Exception as e:
        logger.error(f"TikTok 搜索失败: {keyword} - {e}")
        raise RuntimeError(f"TikTok 搜索失败: {e}") from e
    return videos


def extract_comments_tiktok(video_id: str, max_comments: int = 500) -> list[dict]:
    """TikTok 评论提取 — yt-dlp 暂不支持，返回空列表"""
    logger.warning(f"TikTok 评论抓取暂不支持 (video: {video_id})，跳过")
    return []


# ==================== Facebook (facebook-scraper) ====================

def _get_facebook_cookies() -> dict | None:
    """从数据库读取 Facebook cookies 配置。

    支持两种格式：
    1. JSON 数组（浏览器扩展导出）：[{"name":"c_user","value":"xxx"},...]
    2. 分号分隔字符串：c_user=xxx; xs=xxx
    """
    try:
        from database import get_setting
        cookies_str = get_setting("facebook_cookies", "")
        if not cookies_str:
            return None

        # 尝试 JSON 数组格式（浏览器扩展导出）
        if cookies_str.strip().startswith("["):
            import json as _json
            try:
                arr = _json.loads(cookies_str)
                cookies = {}
                for item in arr:
                    name = item.get("name", "")
                    value = item.get("value", "")
                    if name and value:
                        cookies[name] = value
                if cookies:
                    return cookies
            except (ValueError, TypeError):
                pass

        # 分号分隔格式
        cookies = {}
        for line in cookies_str.replace("\n", ";").split(";"):
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    cookies[k] = v
        return cookies if cookies else None
    except Exception:
        return None


def search_posts_facebook(keyword: str, limit: int = 10) -> list[dict]:
    """
    Facebook 搜索 — 通过 facebook-scraper 抓取公开页面帖子。
    keyword 作为页面名（如 'BlackviewOfficial'）或页面 URL。
    必须配置 cookies 才能获取内容（Facebook 已封锁匿名访问）。
    """
    try:
        from facebook_scraper import get_posts
    except ImportError:
        raise RuntimeError(
            "facebook-scraper 未安装，请运行: pip install facebook-scraper"
        )

    cookies = _get_facebook_cookies()
    if not cookies:
        raise RuntimeError(
            "Facebook 需要配置 cookies 才能抓取内容。请在「设置 → Facebook Cookies 配置」中填入 cookies。"
            "获取方法：浏览器登录 Facebook → 安装 EditThisCookie 扩展 → 导出 cookies（JSON 格式）"
        )
    posts = []
    try:
        pages = max(2, (limit + 3) // 4)
        kwargs = {
            "pages": pages,
            "options": {"comments": False, "progress": True},
            "extra_info": False,
            "cookies": cookies,
        }

        for post in get_posts(keyword, **kwargs):
            if len(posts) >= limit:
                break
            post_id = str(post.get("post_id", ""))
            if not post_id:
                continue
            post_url = post.get("post_url", "")
            posts.append({
                "video_id": post_id,
                "title": (post.get("text") or "")[:200],
                "channel": keyword,
                "view_count": post.get("likes", 0) or 0,
                "comment_count": post.get("comments", 0) or 0,
                "published_at": post.get("time").strftime("%Y%m%d") if post.get("time") else "",
                "source_url": post_url,
                "_url": post_url,
            })
    except Exception as e:
        logger.error(f"Facebook 搜索失败: {keyword} - {e}")
        raise RuntimeError(f"Facebook 搜索失败: {e}") from e
    return posts


def extract_comments_facebook(
    post_id: str, max_comments: int = 500, post_url: str = "", **_kwargs
) -> list[dict]:
    """
    提取 Facebook 帖子评论 — 使用 facebook-scraper。
    需要 post_url 参数（从搜索结果中的 _url 字段获取）。
    配置 cookies 后才能稳定抓取评论。
    """
    try:
        from facebook_scraper import get_posts
    except ImportError:
        raise RuntimeError(
            "facebook-scraper 未安装，请运行: pip install facebook-scraper"
        )

    cookies = _get_facebook_cookies()
    if not cookies:
        raise RuntimeError(
            "Facebook 需要配置 cookies 才能抓取评论。请在「设置 → Facebook Cookies 配置」中填入 cookies。"
        )

    # 优先使用 post_url，其次尝试用 post_id 构造
    url = post_url
    if not url and post_id.startswith("http"):
        url = post_id
    if not url:
        logger.warning(f"Facebook: 无法获取帖子 URL (post_id: {post_id})")
        return []

    comments = []
    try:
        fb_kwargs = {
            "post_urls": [url],
            "options": {"comments": max_comments, "progress": False},
            "extra_info": False,
        }
        if cookies:
            fb_kwargs["cookies"] = cookies

        for post in get_posts(**fb_kwargs):
            for comment in (post.get("comments_full") or []):
                if len(comments) >= max_comments:
                    break
                text = (comment.get("comment_text") or "").strip()
                if not text:
                    continue
                comments.append({
                    "original_id": str(comment.get("comment_id", uuid.uuid4())),
                    "content": text,
                    "author": comment.get("commenter_name", ""),
                    "like_count": comment.get("comment_reaction_count", 0) or 0,
                    "posted_at": str(comment.get("comment_time", "")),
                    "source_url": comment.get("comment_url", ""),
                })
            break  # 只处理第一个帖子
    except Exception as e:
        logger.error(f"Facebook 评论提取失败: {post_id} - {e}")
        raise RuntimeError(f"Facebook 评论提取失败: {e}") from e
    return comments


# ==================== 平台路由 ====================

PLATFORM_REGISTRY = {
    "youtube": {
        "name": "YouTube",
        "search": search_videos_youtube,
        "comments": extract_comments_youtube,
        "comment_supported": True,
        "search_supported": True,
    },
    "reddit": {
        "name": "Reddit",
        "search": search_posts_reddit,
        "comments": extract_comments_reddit,
        "comment_supported": True,
        "search_supported": True,
    },
    "instagram": {
        "name": "Instagram",
        "search": search_posts_instagram,
        "comments": extract_comments_instagram,
        "comment_supported": True,
        "search_supported": True,
    },
    "tiktok": {
        "name": "TikTok",
        "search": search_videos_tiktok,
        "comments": extract_comments_tiktok,
        "comment_supported": False,
        "search_supported": True,
    },
    "facebook": {
        "name": "Facebook",
        "search": search_posts_facebook,
        "comments": extract_comments_facebook,
        "comment_supported": True,
        "search_supported": True,
    },
    "aliexpress": {
        "name": "AliExpress",
        "search": search_products_aliexpress,
        "comments": extract_comments_aliexpress,
        "comment_supported": True,
        "search_supported": True,
    },
}


def list_platforms() -> list[dict]:
    """返回支持的平台列表"""
    platform_libs = {
        "youtube": "yt-dlp",
        "reddit": "PRAW (官方API) + Cookie 认证",
        "instagram": "Instaloader（需登录账号）",
        "tiktok": "yt-dlp",
        "facebook": "facebook-scraper（需 cookies）",
        "aliexpress": "feedback.aliexpress.com 公开接口",
    }
    platform_hints = {
        "youtube": "通过 yt-dlp 搜索视频并提取评论",
        "reddit": "优先使用 PRAW 官方 API（需配置 OAuth 凭据），备选 Cookie 认证；认证失败时快速失败",
        "instagram": "通过 Instaloader 按 hashtag 搜索帖子并提取评论（必须配置账号密码，Instagram 已封锁匿名访问）",
        "tiktok": "通过 yt-dlp 搜索视频元数据，评论抓取暂不支持",
        "facebook": "通过 facebook-scraper 抓取公开页面帖子和评论（必须配置 cookies，Facebook 已封锁匿名访问）",
        "aliexpress": "关键词处粘贴商品链接或商品 ID（逗号分隔多个），抓取商品评论（多语言原样入库）",
    }
    return [
        {"id": pid, "name": p["name"],
         "comment_supported": p["comment_supported"],
         "search_supported": p["search_supported"],
         "engine": platform_libs.get(pid, ""),
         "hint": platform_hints.get(pid, "")}
        for pid, p in PLATFORM_REGISTRY.items()
    ]


def crawl_competitor(
    brand_name: str,
    search_keyword: str,
    max_videos: int = 5,
    platform: str = "youtube",
    progress_callback=None,
    cancel_callback=None,
) -> dict:
    """
    抓取一个竞品在指定平台的评论
    platform: youtube / reddit / instagram / tiktok
    """
    pconfig = PLATFORM_REGISTRY.get(platform)
    if not pconfig:
        return {"status": "failed", "error": f"不支持的平台: {platform}"}

    logger.info(f"开始抓取 [{pconfig['name']}] {brand_name} (关键词: {search_keyword})")

    brand_id = insert_brand(brand_name, search_keyword)

    # 搜索视频/帖子
    errors = []
    try:
        videos = pconfig["search"](search_keyword, limit=max_videos)
    except Exception as exc:
        logger.exception("[%s] 搜索失败", brand_name)
        videos = []
        errors.append({"stage": "search", "error": f"{type(exc).__name__}: {exc}"})
    logger.info(f"  找到 {len(videos)} 个帖子/视频")

    # Reddit: 搜索和评论之间加延迟，避免连续请求触发 429
    if platform == "reddit" and videos:
        time.sleep(_REDDIT_DELAY)

    total_comments = 0
    new_comments = 0
    filtered_comments = 0

    for index, video in enumerate(videos, 1):
        if cancel_callback and cancel_callback():
            return {
                "platform": pconfig["name"], "brand": brand_name,
                "videos_found": len(videos), "comments_extracted": total_comments,
                "new_comments": new_comments, "cancelled": True, "errors": errors,
            }
        video_db_id = save_video(video, brand_id, platform=platform)

        if pconfig["comment_supported"]:
            # Reddit 和 Facebook 需要额外传入 post_url 参数构建评论请求
            try:
                if platform == "reddit":
                    subreddit = video.get("_subreddit", "")
                    post_url = video.get("_url", "")
                    comments = pconfig["comments"](
                        video["video_id"],
                        max_comments=settings.MAX_COMMENTS_PER_VIDEO,
                        subreddit=subreddit,
                        post_url=post_url,
                    )
                elif platform == "facebook":
                    post_url = video.get("_url", "") or video.get("source_url", "")
                    comments = pconfig["comments"](
                        video["video_id"],
                        max_comments=settings.MAX_COMMENTS_PER_VIDEO,
                        post_url=post_url,
                    )
                else:
                    comments = pconfig["comments"](
                        video["video_id"],
                        max_comments=settings.MAX_COMMENTS_PER_VIDEO,
                    )
            except Exception as exc:
                logger.exception("[%s] 帖子 %s 评论提取失败", brand_name, video.get("video_id"))
                comments = []
                errors.append({
                    "stage": "comments", "video_id": video.get("video_id", ""),
                    "error": f"{type(exc).__name__}: {exc}",
                })
        else:
            comments = []

        total, new, filtered = _store_comments(comments, video_db_id, brand_id, platform)
        total_comments += total
        new_comments += new
        filtered_comments += filtered

        logger.info(f"  帖子 {video['video_id']}: 提取 {len(comments)} 条评论 (通过 {total}, 新增 {new}, 过滤 {filtered})")
        if progress_callback:
            progress_callback(index, len(videos), f"[{brand_name}] 已处理 {index}/{len(videos)} 个帖子")

        # Reddit: 帖子间加延迟，避免连续请求触发 429
        if platform == "reddit" and video is not videos[-1]:
            time.sleep(_REDDIT_DELAY)

    status = "failed" if errors else ("empty" if not videos else "succeeded")
    return {
        "status": status,
        "platform": pconfig["name"],
        "brand": brand_name,
        "videos_found": len(videos),
        "comments_extracted": total_comments,
        "new_comments": new_comments,
        "comments_filtered": filtered_comments,
        "errors": errors,
    }


def _crawl_all_brands_reddit_combined_legacy(
    brands: list[dict],
    search_keyword: str,
    max_videos: int = 5,
    progress_callback=None,
    cancel_callback=None,
) -> list[dict]:
    """
    Reddit 全品牌抓取优化：用 OR 查询一次搜索所有品牌名，去重后只提取一次评论。
    将 9 次搜索降为 1 次，大幅减少 429 限流风险。
    """
    logger.info(f"[Reddit 全品牌] 品牌数: {len(brands)}, 关键词: {search_keyword}")

    # 1. 构建组合搜索查询：品牌名1 OR 品牌名2 OR ... [用户关键词]
    brand_names = [b["name"] for b in brands]
    # Reddit 搜索支持 OR 语法
    brand_query = " OR ".join(brand_names)
    # 如果用户也提供了关键词，附加到查询中
    if search_keyword:
        combined_query = f"({brand_query}) AND ({search_keyword})"
    else:
        combined_query = brand_query

    logger.info(f"[Reddit 全品牌] 组合查询: {combined_query[:100]}...")

    # 2. 通过恢复的 JSON 主路径搜索；必要时由 search_posts_reddit_v2 使用 RSS 备用。
    try:
        posts = search_posts_reddit(combined_query, limit=max_videos * 3)
    except RedditFetchError as exc:
        logger.error("[Reddit 全品牌] 搜索失败: %s", exc)
        return [{"platform": "Reddit", "brand": b["name"], "videos_found": 0,
                 "comments_extracted": 0, "new_comments": 0,
                 "errors": [{"stage": "search", "error": str(exc)}]} for b in brands]

    logger.info(f"[Reddit 全品牌] 搜索到 {len(posts)} 个结果")

    # 搜索和评论之间加延迟，避免连续请求触发 429
    time.sleep(_REDDIT_DELAY)

    # 3. 为每个品牌创建记录
    brand_ids = {}
    for b in brands:
        brand_ids[b["name"]] = insert_brand(b["name"], b.get("search_keyword", b["name"]))

    # 4. 匹配帖子到品牌：检查标题中是否包含品牌名
    all_posts: dict[str, dict] = {}  # post_id → {post, brands: [...]}
    for post in posts:
        post_id = post.get("video_id", "")
        if not post_id:
            continue
        if post_id in all_posts:
            continue

        # 检查标题和内容中包含哪些品牌名
        title_lower = post.get("title", "").lower()
        content_lower = post.get("content", "").lower()
        matched_brands = []
        for b in brands:
            bname = b["name"].lower()
            if bname in title_lower or bname in content_lower:
                matched_brands.append(b["name"])

        if not matched_brands:
            # 帖子没有明确提到任何品牌，跳过（避免无效数据）
            continue

        all_posts[post_id] = {"post": post, "brands": matched_brands}

    logger.info(f"[Reddit 全品牌] 匹配到 {len(all_posts)} 个帖子")

    if not all_posts:
        return [{"platform": "Reddit", "brand": b["name"], "videos_found": 0,
                 "comments_extracted": 0, "new_comments": 0} for b in brands]

    # 5. 初始化每个品牌的统计
    brand_stats = {b["name"]: {"videos_found": 0, "comments_extracted": 0, "new_comments": 0, "errors": []}
                   for b in brands}

    # 6. 为每个唯一帖子提取评论，并分发给匹配的品牌
    post_list = list(all_posts.values())
    for idx, item in enumerate(post_list):
        if cancel_callback and cancel_callback():
            logger.info("[Reddit 全品牌] 收到取消请求")
            break
        post = item["post"]
        matched_brands = item["brands"]

        # 提取评论（每个帖子只提取一次）
        subreddit = post.get("_subreddit", "")
        post_url = post.get("_url", "")
        try:
            comments = extract_comments_reddit(
                post["video_id"],
                max_comments=settings.MAX_COMMENTS_PER_VIDEO,
                subreddit=subreddit,
                post_url=post_url,
            )
        except Exception as exc:
            logger.exception("[Reddit 全品牌] 帖子 %s 评论提取失败", post["video_id"])
            comments = []
            for brand_name in matched_brands:
                brand_stats[brand_name]["errors"].append({
                    "stage": "comments", "video_id": post["video_id"],
                    "error": f"{type(exc).__name__}: {exc}",
                })

        # 分发给每个匹配的品牌
        for brand_name in matched_brands:
            brand_id = brand_ids[brand_name]
            video_db_id = save_video(post, brand_id, platform="reddit")
            total, new, filtered = _store_comments(comments, video_db_id, brand_id, "reddit")
            brand_stats[brand_name]["videos_found"] += 1
            brand_stats[brand_name]["comments_extracted"] += total
            brand_stats[brand_name]["new_comments"] += new
            brand_stats[brand_name]["comments_filtered"] = brand_stats[brand_name].get("comments_filtered", 0) + filtered

        logger.info(f"  [Reddit 全品牌] 帖子 {post['video_id']} ({','.join(matched_brands)}): {len(comments)} 条评论")
        if progress_callback:
            progress_callback(idx + 1, len(post_list), f"Reddit 已处理 {idx + 1}/{len(post_list)} 个帖子")

        # 帖子间延迟
        if idx < len(post_list) - 1:
            time.sleep(_REDDIT_DELAY)

    logger.info(f"[Reddit 全品牌] 完成，处理 {len(post_list)} 个帖子")

    return [
        {
            "platform": "Reddit",
            "brand": b["name"],
            "videos_found": brand_stats[b["name"]]["videos_found"],
            "comments_extracted": brand_stats[b["name"]]["comments_extracted"],
            "new_comments": brand_stats[b["name"]]["new_comments"],
            "comments_filtered": brand_stats[b["name"]].get("comments_filtered", 0),
            "errors": brand_stats[b["name"]]["errors"],
        }
        for b in brands
    ]


def crawl_all_brands_reddit(
    brands: list[dict],
    search_keyword: str,
    max_videos: int = 5,
    progress_callback=None,
    cancel_callback=None,
) -> list[dict]:
    """Search Reddit brand by brand so one broad query cannot hide matches."""
    results = []
    total_brands = len(brands)
    for index, brand in enumerate(brands, 1):
        if cancel_callback and cancel_callback():
            break
        brand_name = (brand.get("name") or "").strip()
        configured_keyword = (brand.get("search_keyword") or "").strip()
        requested_keyword = (search_keyword or "").strip()
        if requested_keyword:
            keyword = requested_keyword
            if brand_name and brand_name.lower() not in keyword.lower():
                keyword = f"{brand_name} {keyword}"
        else:
            keyword = configured_keyword or brand_name

        def report(current, total, message, brand_index=index):
            if progress_callback:
                completed = (brand_index - 1) * 100 + round(current / max(total, 1) * 100)
                progress_callback(completed, max(total_brands * 100, 1), message)

        result = crawl_competitor(
            brand_name,
            keyword,
            max_videos=max_videos,
            platform="reddit",
            progress_callback=report,
            cancel_callback=cancel_callback,
        )
        results.append(result)
        if progress_callback:
            progress_callback(index * 100, max(total_brands * 100, 1),
                              f"Reddit 已完成 {index}/{total_brands} 个品牌")
        if index < total_brands and not (cancel_callback and cancel_callback()):
            time.sleep(_REDDIT_DELAY)
    return results


if __name__ == "__main__":
    import json as _json
    logging.basicConfig(level=logging.INFO)
    # 测试 Reddit
    result = crawl_competitor("Blackview", "Blackview rugged phone", max_videos=2, platform="reddit")
    print(_json.dumps(result, indent=2, ensure_ascii=False))
