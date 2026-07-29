"""
多平台评论抓取模块
支持: YouTube (yt-dlp), Reddit (JSON API), Instagram (yt-dlp+cookies), TikTok (元数据)
"""
import re
import uuid
import logging
import json
import time
from yt_dlp import YoutubeDL
from config import settings
from database import get_db, insert_comment, insert_brand

logger = logging.getLogger(__name__)

# HTTP 请求公共头
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9",
}


# ==================== 文本处理工具 ====================

def clean_comment(text: str) -> str:
    """清洗评论文本"""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_language(text: str) -> str:
    """检测评论语言"""
    try:
        from langdetect import detect
        return detect(text)
    except Exception:
        return "en"


# ==================== 数据库操作 ====================

def save_video(video: dict, brand_id: str, platform: str = "youtube") -> str:
    """保存视频/帖子信息到数据库，返回 video_id"""
    conn = get_db()
    video_db_id = str(uuid.uuid4())
    try:
        conn.execute("""
            INSERT OR IGNORE INTO videos
                (id, video_id, title, channel, view_count, comment_count, published_at, brand_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            video_db_id,
            video["video_id"],
            video.get("title", ""),
            video.get("channel", ""),
            video.get("view_count", 0),
            video.get("comment_count", 0),
            video.get("published_at", ""),
            brand_id,
        ))
        conn.commit()
    finally:
        conn.close()

    conn = get_db()
    row = conn.execute(
        "SELECT id FROM videos WHERE video_id = ?", (video["video_id"],)
    ).fetchone()
    conn.close()
    return row["id"] if row else video_db_id


def _store_comments(comments: list[dict], video_db_id: str, brand_id: str, platform: str) -> tuple[int, int]:
    """存储评论到数据库，返回 (总数, 新增数)"""
    total = 0
    new_count = 0
    for c in comments:
        text = c.get("content", "").strip()
        if not text or len(text) < 5:
            continue
        cleaned = clean_comment(text)
        if not cleaned:
            continue
        lang = detect_language(cleaned)
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
            "language": lang,
        })
        total += 1
        if is_new:
            new_count += 1
    return total, new_count


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
                })
    except Exception as e:
        logger.error(f"YouTube 搜索失败: {keyword} - {e}")
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
                })
    except Exception as e:
        logger.error(f"YouTube 评论提取失败: {video_id} - {e}")
    return comments


# ==================== Reddit ====================

def search_posts_reddit(keyword: str, limit: int = 10) -> list[dict]:
    """用 Reddit JSON API 搜索帖子"""
    import requests
    url = "https://www.reddit.com/search.json"
    params = {"q": keyword, "limit": limit, "sort": "relevance", "type": "link"}
    posts = []
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            post_id = d.get("id", "")
            if not post_id:
                continue
            posts.append({
                "video_id": post_id,  # 复用 video_id 字段存 Reddit post id
                "title": d.get("title", ""),
                "channel": f"r/{d.get('subreddit', '')}",
                "view_count": d.get("score", 0) or 0,
                "comment_count": d.get("num_comments", 0) or 0,
                "published_at": time.strftime("%Y%m%d", time.gmtime(d.get("created_utc", 0))) if d.get("created_utc") else "",
            })
    except Exception as e:
        logger.error(f"Reddit 搜索失败: {keyword} - {e}")
    return posts


def extract_comments_reddit(post_id: str, max_comments: int = 500) -> list[dict]:
    """用 Reddit JSON API 提取帖子评论"""
    import requests
    url = f"https://www.reddit.com/comments/{post_id}.json"
    params = {"limit": max_comments}
    comments = []
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # data[1].data.children 是评论列表
        if len(data) >= 2:
            for child in data[1].get("data", {}).get("children", []):
                c = child.get("data", {})
                body = c.get("body", "").strip()
                if not body or body == "[deleted]" or body == "[removed]":
                    continue
                comments.append({
                    "original_id": c.get("id", str(uuid.uuid4())),
                    "content": body,
                    "author": c.get("author", ""),
                    "like_count": c.get("score", 0) or 0,
                    "posted_at": str(c.get("created_utc", "")),
                })
    except Exception as e:
        logger.error(f"Reddit 评论提取失败: {post_id} - {e}")
    return comments


# ==================== Instagram (Instaloader) ====================

def search_posts_instagram(keyword: str, limit: int = 10) -> list[dict]:
    """
    Instagram 搜索 — 通过 Instaloader 按 hashtag 搜索帖子
    Instaloader 纯 HTTP 实现，无需浏览器自动化
    """
    tag = re.sub(r"[^a-zA-Z0-9]", "", keyword).lower()
    if not tag:
        return []
    posts = []
    try:
        import instaloader
        L = instaloader.Instaloader(
            download_posts=False, download_videos=False,
            download_video_thumbnails=False, save_metadata=False,
            post_metadata_txt_pattern="", quiet=True,
        )
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
            })
            count += 1
    except Exception as e:
        logger.error(f"Instagram 搜索失败: {keyword} - {e}")
    return posts


def extract_comments_instagram(shortcode: str, max_comments: int = 500) -> list[dict]:
    """
    提取 Instagram 帖子评论 — 使用 Instaloader
    Instaloader.Post.get_comments() 返回 Comment 对象列表
    """
    comments = []
    try:
        import instaloader
        L = instaloader.Instaloader(
            download_posts=False, download_videos=False,
            download_video_thumbnails=False, save_metadata=False,
            post_metadata_txt_pattern="", quiet=True,
        )
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
            })
            count += 1
    except Exception as e:
        logger.error(f"Instagram 评论提取失败: {shortcode} - {e}")
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
                })
    except Exception as e:
        logger.error(f"TikTok 搜索失败: {keyword} - {e}")
    return videos


def extract_comments_tiktok(video_id: str, max_comments: int = 500) -> list[dict]:
    """TikTok 评论提取 — yt-dlp 暂不支持，返回空列表"""
    logger.warning(f"TikTok 评论抓取暂不支持 (video: {video_id})，跳过")
    return []


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
}


def list_platforms() -> list[dict]:
    """返回支持的平台列表"""
    platform_libs = {
        "youtube": "yt-dlp",
        "reddit": "Reddit JSON API",
        "instagram": "Instaloader",
        "tiktok": "yt-dlp",
    }
    platform_hints = {
        "youtube": "通过 yt-dlp 搜索视频并提取评论",
        "reddit": "通过 Reddit JSON API 搜索帖子并提取评论（无需认证）",
        "instagram": "通过 Instaloader 按 hashtag 搜索帖子并提取评论",
        "tiktok": "通过 yt-dlp 搜索视频元数据，评论抓取暂不支持",
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
) -> dict:
    """
    抓取一个竞品在指定平台的评论
    platform: youtube / reddit / instagram / tiktok
    """
    pconfig = PLATFORM_REGISTRY.get(platform)
    if not pconfig:
        return {"error": f"不支持的平台: {platform}"}

    logger.info(f"开始抓取 [{pconfig['name']}] {brand_name} (关键词: {search_keyword})")

    brand_id = insert_brand(brand_name, search_keyword)

    # 搜索视频/帖子
    videos = pconfig["search"](search_keyword, limit=max_videos)
    logger.info(f"  找到 {len(videos)} 个帖子/视频")

    total_comments = 0
    new_comments = 0

    for video in videos:
        video_db_id = save_video(video, brand_id, platform=platform)

        if pconfig["comment_supported"]:
            comments = pconfig["comments"](
                video["video_id"],
                max_comments=settings.MAX_COMMENTS_PER_VIDEO,
            )
        else:
            comments = []

        total, new = _store_comments(comments, video_db_id, brand_id, platform)
        total_comments += total
        new_comments += new

        logger.info(f"  帖子 {video['video_id']}: 提取 {len(comments)} 条评论 (新增 {new})")

    return {
        "platform": pconfig["name"],
        "brand": brand_name,
        "videos_found": len(videos),
        "comments_extracted": total_comments,
        "new_comments": new_comments,
    }


if __name__ == "__main__":
    import json as _json
    logging.basicConfig(level=logging.INFO)
    # 测试 Reddit
    result = crawl_competitor("Blackview", "Blackview rugged phone", max_videos=2, platform="reddit")
    print(_json.dumps(result, indent=2, ensure_ascii=False))
