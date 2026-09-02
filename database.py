"""
数据库层 — SQLite 存储
MVP 阶段用 SQLite，后续可平滑迁移到 PostgreSQL
"""
import sqlite3
import uuid
import json
import base64
import ctypes
import logging
import os
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from config import settings

logger = logging.getLogger(__name__)

ALLOWED_PAIN_CATEGORIES = {"hardware", "software", "scenario", "ecosystem"}
ALLOWED_PAIN_TAGS = {
    "battery", "screen", "waterproof", "system", "weight", "signal",
    "camera", "button", "charging", "durability", "app_pairing", "ota",
    "ui", "delay",
}
ALLOWED_BRAND_TYPES = {"competitor", "own", "benchmark"}
ALLOWED_EMOTION_TYPES = {"anger", "disappointment", "satisfaction", "surprise", "neutral"}


def normalize_analysis_result(result: dict) -> dict:
    """Normalize and validate the persisted AI schema."""
    if not isinstance(result, dict):
        raise ValueError("分析结果必须是 JSON 对象")
    required = ("sentiment_score", "pain_categories", "pain_tags", "severity", "summary_zh")
    missing = [field for field in required if field not in result]
    if missing:
        raise ValueError(f"分析结果缺少字段: {', '.join(missing)}")

    def integer(name: str, low: int, high: int) -> int:
        value = result.get(name)
        if isinstance(value, bool):
            raise ValueError(f"{name} 必须是整数")
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 必须是整数") from None
        if isinstance(value, float) and parsed != value:
            raise ValueError(f"{name} 必须是整数")
        if isinstance(value, str) and str(parsed) != value.strip():
            raise ValueError(f"{name} 必须是整数")
        if not low <= parsed <= high:
            raise ValueError(f"{name} 必须在 {low}-{high} 之间")
        return parsed

    def enum_list(name: str, allowed: set[str]) -> list[str]:
        values = result.get(name)
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise ValueError(f"{name} 必须是字符串数组")
        cleaned = []
        for item in values:
            item = item.strip()
            if item and item not in allowed:
                raise ValueError(f"{name} 包含不支持的值: {item}")
            if item and item not in cleaned:
                cleaned.append(item)
        return cleaned

    summary = result.get("summary_zh")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary_zh 不能为空")
    confidence = result.get("confidence", 0.5)
    if isinstance(confidence, bool):
        raise ValueError("confidence 必须是数字")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        raise ValueError("confidence 必须是 0-1 之间的数字") from None
    if not 0 <= confidence <= 1:
        raise ValueError("confidence 必须在 0-1 之间")

    normalized = dict(result)
    normalized["sentiment_score"] = integer("sentiment_score", 1, 5)
    normalized["severity"] = integer("severity", 1, 3)
    normalized["pain_categories"] = enum_list("pain_categories", ALLOWED_PAIN_CATEGORIES)
    normalized["pain_tags"] = enum_list("pain_tags", ALLOWED_PAIN_TAGS)
    normalized["summary_zh"] = summary.strip()[:500]
    normalized["confidence"] = round(confidence, 4)
    for field in ("translation_zh", "user_solution", "product_match"):
        value = normalized.get(field)
        if value is not None and not isinstance(value, str):
            normalized[field] = str(value)
    normalized.setdefault("translation_zh", "")
    normalized.setdefault("user_solution", None)
    normalized.setdefault("product_match", None)

    # 四元组字段（可选，默会知识显性化）
    for field in ("context_environment", "hardware_component", "user_action", "pain_root_cause"):
        value = normalized.get(field)
        if value is not None and not isinstance(value, str):
            normalized[field] = str(value)
        normalized.setdefault(field, None)

    # 黄帽：正面反馈标签（可选）
    ptags = normalized.get("positive_tags")
    if ptags is not None:
        if isinstance(ptags, str):
            ptags = [ptags]
        if not isinstance(ptags, list) or any(not isinstance(i, str) for i in ptags):
            raise ValueError("positive_tags 必须是字符串数组")
        normalized["positive_tags"] = [t.strip() for t in ptags if t.strip()]
    else:
        normalized.setdefault("positive_tags", [])

    # 红帽：情绪类型（可选）
    etype = normalized.get("emotion_type")
    if etype is not None:
        etype = str(etype).strip()
        if etype and etype not in ALLOWED_EMOTION_TYPES:
            raise ValueError(f"emotion_type 包含不支持的值: {etype}")
    normalized.setdefault("emotion_type", None)
    return normalized


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_transform_bytes(raw: bytes, protect: bool) -> bytes | None:
    """Use Windows DPAPI without adding a third-party dependency."""
    if os.name != "nt" or not raw:
        return None
    try:
        input_buffer = ctypes.create_string_buffer(raw)
        input_blob = _DATA_BLOB(
            len(raw), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_char))
        )
        output_blob = _DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if protect:
            ok = crypt32.CryptProtectData(
                ctypes.byref(input_blob), None, None, None, None, 0,
                ctypes.byref(output_blob)
            )
        else:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(input_blob), None, None, None, None, 0,
                ctypes.byref(output_blob)
            )
        if not ok:
            return None
        try:
            transformed = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)
        return transformed
    except Exception as exc:
        logger.warning("Windows DPAPI operation failed: %s", exc)
        return None


def _is_sensitive_key(key: str) -> bool:
    return key.endswith("_api_key") or key.endswith("_secret") or key.endswith("_password") or key.endswith("_cookies")


def _protect_setting(key: str, value: str) -> str:
    if not value or not _is_sensitive_key(key) or value.startswith("dpapi:"):
        return value
    encrypted = _dpapi_transform_bytes(value.encode("utf-8"), protect=True)
    return f"dpapi:{base64.b64encode(encrypted).decode('ascii')}" if encrypted else value


def _unprotect_setting(key: str, value: str) -> str:
    if not value or not _is_sensitive_key(key) or not value.startswith("dpapi:"):
        return value
    encoded = value[6:]
    try:
        raw = base64.b64decode(encoded.encode("ascii"))
        decrypted = _dpapi_transform_bytes(raw, protect=False)
        return decrypted.decode("utf-8") if decrypted is not None else value
    except Exception:
        return value


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str):
    """为已有的 SQLite 数据库补充新字段，保持 MVP 升级兼容。"""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _json_or_default(value, default=None):
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def get_db() -> sqlite3.Connection:
    """获取数据库连接"""
    conn = sqlite3.connect(str(settings.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """初始化数据库表结构"""
    conn = get_db()
    cursor = conn.cursor()

    # 品牌表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS brands (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            search_keyword TEXT NOT NULL,
            brand_type TEXT NOT NULL DEFAULT 'competitor',
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # 产品表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            brand_id TEXT NOT NULL REFERENCES brands(id),
            model TEXT NOT NULL,
            aliases TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(brand_id, model)
        )
    """)

    # 视频表（YouTube 视频元数据）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            video_id TEXT UNIQUE NOT NULL,
            platform TEXT NOT NULL DEFAULT 'youtube',
            external_id TEXT,
            title TEXT,
            channel TEXT,
            view_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            published_at TEXT,
            source_url TEXT,
            crawled_at TEXT DEFAULT (datetime('now')),
            brand_id TEXT REFERENCES brands(id)
        )
    """)

    # 评论表（原始评论）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL DEFAULT 'youtube',
            original_id TEXT NOT NULL,
            video_id TEXT REFERENCES videos(id),
            brand_id TEXT REFERENCES brands(id),
            content TEXT NOT NULL,
            content_clean TEXT,
            language TEXT,
            author TEXT,
            like_count INTEGER DEFAULT 0,
            posted_at TEXT,
            source_url TEXT,
            crawled_at TEXT DEFAULT (datetime('now')),
            sentiment_pre INTEGER,
            analyzed INTEGER DEFAULT 0,
            UNIQUE(platform, original_id)
        )
    """)

    # 分析结果表
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS analyses (
            id TEXT PRIMARY KEY,
            comment_id TEXT NOT NULL REFERENCES comments(id),
            sentiment_score INTEGER NOT NULL,
            pain_categories TEXT NOT NULL,
            pain_tags TEXT NOT NULL,
            severity INTEGER NOT NULL,
            user_solution TEXT,
            product_match TEXT,
            translation_zh TEXT,
            summary_zh TEXT NOT NULL,
            confidence REAL,
            llm_model TEXT NOT NULL,
            prompt_version TEXT,
            analyzed_at TEXT DEFAULT (datetime('now')),
            human_corrected INTEGER DEFAULT 0,
            UNIQUE(comment_id)
        )
    """)

    # 配置表（存 LLM 提供商、API Key 等设置）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # 后台抓取/分析任务。结果以 JSON 保存，单机版重启后仍可查看历史。
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            params TEXT,
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            result_json TEXT,
            error TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            started_at TEXT,
            completed_at TEXT
        )
    """)

    # 竞品硬件规格库（v1.0 新增）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS competitor_specs (
            id TEXT PRIMARY KEY,
            brand_id TEXT REFERENCES brands(id),
            product_id TEXT,
            model TEXT,
            spec_category TEXT NOT NULL,
            spec_key TEXT NOT NULL,
            spec_value TEXT NOT NULL,
            spec_unit TEXT,
            source_url TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # 打标基准集（v1.0 新增）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gold_standard (
            id TEXT PRIMARY KEY,
            comment_id TEXT NOT NULL REFERENCES comments(id),
            expected_tags TEXT,
            expected_severity INTEGER,
            expected_sentiment INTEGER,
            expected_four_tuple TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(comment_id)
        )
    """)

    # 痛点聚类主题（v2.0 Phase 2 使用，表结构先建保证向前兼容）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clusters (
            id TEXT PRIMARY KEY,
            model_version TEXT,
            topic_name TEXT,
            topic_name_en TEXT,
            description TEXT,
            keywords TEXT,
            comment_count INTEGER NOT NULL DEFAULT 0,
            representative_comment_ids TEXT,
            avg_severity REAL,
            avg_sentiment REAL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # AI 改良建议（v2.0 Phase 3 使用；evidence_comment_ids 由应用层强制非空）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS suggestions (
            id TEXT PRIMARY KEY,
            report_id TEXT REFERENCES reports(id),
            cluster_id TEXT,
            title TEXT NOT NULL,
            priority_score REAL,
            priority_factors TEXT,
            evidence_comment_ids TEXT NOT NULL,
            evidence_quotes TEXT,
            affected_brands TEXT,
            spec_hint TEXT,
            effort TEXT,
            opportunity TEXT,
            detail_md TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # 建议报告版本（v2.0 Phase 3 使用）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            title TEXT,
            params TEXT,
            content_md TEXT,
            suggestion_ids TEXT,
            model TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # 评论向量缓存（v2.0 Phase 2：聚类增量运行的核心，避免重复 embedding）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            comment_id TEXT NOT NULL REFERENCES comments(id),
            model_version TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vector BLOB NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (comment_id, model_version)
        )
    """)

    # 评论中文翻译缓存（v1.2.2：证据/簇内评论展示用，翻译一次永久复用）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS comment_translations (
            comment_id TEXT PRIMARY KEY REFERENCES comments(id),
            translation_zh TEXT NOT NULL,
            llm_model TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # 兼容 v0.5 已存在的数据库；CREATE TABLE 不会自动补列。
    _ensure_column(conn, "brands", "brand_type", "TEXT NOT NULL DEFAULT 'competitor'")
    _ensure_column(conn, "videos", "platform", "TEXT NOT NULL DEFAULT 'youtube'")
    _ensure_column(conn, "videos", "external_id", "TEXT")
    _ensure_column(conn, "videos", "source_url", "TEXT")
    _ensure_column(conn, "comments", "source_url", "TEXT")
    # v2.0 质量过滤 + 聚类预留列（只加不删，旧数据兼容）
    _ensure_column(conn, "comments", "quality_score", "REAL")
    _ensure_column(conn, "comments", "filtered", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "comments", "filter_reason", "TEXT")
    _ensure_column(conn, "comments", "parent_id", "TEXT")
    _ensure_column(conn, "comments", "depth", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "comments", "meta_json", "TEXT")
    _ensure_column(conn, "comments", "cluster_id", "TEXT")
    _ensure_column(conn, "analyses", "translation_zh", "TEXT")
    _ensure_column(conn, "analyses", "confidence", "REAL")
    _ensure_column(conn, "analyses", "prompt_version", "TEXT")
    _ensure_column(conn, "analyses", "context_environment", "TEXT")
    _ensure_column(conn, "analyses", "hardware_component", "TEXT")
    _ensure_column(conn, "analyses", "user_action", "TEXT")
    _ensure_column(conn, "analyses", "pain_root_cause", "TEXT")
    _ensure_column(conn, "analyses", "positive_tags", "TEXT")
    _ensure_column(conn, "analyses", "emotion_type", "TEXT")
    _ensure_column(conn, "analyses", "human_corrected", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "jobs", "cancel_requested", "INTEGER NOT NULL DEFAULT 0")

    # 索引
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_brand ON comments(brand_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_analyzed ON comments(analyzed)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_platform_original ON comments(platform, original_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_cluster ON comments(cluster_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_version ON embeddings(model_version)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_videos_platform_video ON videos(platform, video_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_videos_platform_external ON videos(platform, external_id)")
    # v0.5 的视频 ID 没有平台命名空间；迁移为 platform:external_id，
    # 评论表引用的是 videos.id，因此不会破坏已有评论关联。
    cursor.execute("""
        UPDATE videos
        SET external_id = CASE
                WHEN external_id IS NULL OR external_id = '' THEN video_id
                ELSE external_id
            END,
            video_id = CASE
                WHEN instr(video_id, ':') = 0 THEN platform || ':' || video_id
                ELSE video_id
            END
        WHERE instr(video_id, ':') = 0 OR external_id IS NULL OR external_id = ''
    """)
    # v0.5 stored Reddit post IDs in the YouTube-shaped table without a
    # platform column. Reddit IDs are short base36 strings (usually 5-8
    # characters), while YouTube video IDs are 11 characters. Repair only
    # those legacy rows so existing comment relationships remain intact.
    cursor.execute("""
        UPDATE videos
        SET platform = 'reddit',
            video_id = 'reddit:' || external_id
        WHERE platform = 'youtube'
          AND length(external_id) BETWEEN 5 AND 8
          AND video_id LIKE 'youtube:%'
    """)
    cursor.execute("""
        UPDATE comments
        SET platform = (SELECT v.platform FROM videos v WHERE v.id = comments.video_id)
        WHERE video_id IN (SELECT id FROM videos WHERE platform = 'reddit')
    """)
    cursor.execute("""
        UPDATE videos
        SET source_url = 'https://www.reddit.com/comments/' || external_id || '/'
        WHERE platform = 'reddit' AND (source_url IS NULL OR source_url LIKE 'https://www.youtube.com/%')
    """)
    cursor.execute("""
        UPDATE videos
        SET source_url = CASE platform
            WHEN 'youtube' THEN 'https://www.youtube.com/watch?v=' || external_id
            WHEN 'reddit' THEN 'https://www.reddit.com/comments/' || external_id || '/'
            WHEN 'instagram' THEN 'https://www.instagram.com/p/' || external_id || '/'
            WHEN 'tiktok' THEN 'https://www.tiktok.com/video/' || external_id
            ELSE source_url
        END
        WHERE (source_url IS NULL OR source_url = '') AND external_id IS NOT NULL
    """)
    cursor.execute("""
        UPDATE comments
        SET source_url = 'https://www.youtube.com/watch?v=' ||
            substr((SELECT v.video_id FROM videos v WHERE v.id = comments.video_id), 9) ||
            '&lc=' || original_id
        WHERE (source_url IS NULL OR source_url = '') AND platform = 'youtube'
    """)
    cursor.execute("""
        UPDATE comments
        SET source_url = 'https://www.reddit.com/comments/' ||
            (SELECT v.external_id FROM videos v WHERE v.id = comments.video_id) ||
            '/'
        WHERE (source_url IS NULL OR source_url = '') AND platform = 'reddit'
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_analyses_severity ON analyses(severity)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_analyses_sentiment ON analyses(sentiment_score)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_filtered ON comments(filtered)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_cluster ON comments(cluster_id)")

    # A process restart cannot resume daemon threads safely. Preserve the
    # history while making abandoned jobs explicit to the user.
    cursor.execute("""
        UPDATE jobs
        SET status = 'failed',
            error = COALESCE(error, '应用重启导致任务中断'),
            message = '应用重启导致任务中断',
            completed_at = COALESCE(completed_at, datetime('now'))
        WHERE status IN ('queued', 'running')
    """)

    cursor.execute("""
        INSERT INTO settings (key, value, updated_at)
        VALUES ('schema_version', '4', datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value = '4', updated_at = datetime('now')
    """)
    # Migrate legacy plaintext API keys on Windows as soon as DPAPI is
    # available. Non-Windows development environments keep the legacy value.
    if os.name == "nt":
        key_rows = cursor.execute("SELECT key, value FROM settings WHERE key LIKE '%_api_key'").fetchall()
        for row in key_rows:
            protected = _protect_setting(row["key"], row["value"] or "")
            if protected != row["value"]:
                cursor.execute(
                    "UPDATE settings SET value = ?, updated_at = datetime('now') WHERE key = ?",
                    (protected, row["key"]),
                )

    conn.commit()
    conn.close()


def insert_brand(name: str, search_keyword: str, brand_type: str = "competitor") -> str:
    """插入品牌，返回 brand_id"""
    conn = get_db()
    brand_id = str(uuid.uuid4())
    try:
        conn.execute(
            "INSERT INTO brands (id, name, search_keyword, brand_type) VALUES (?, ?, ?, ?)",
            (brand_id, name, search_keyword, brand_type)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # 品牌已存在，返回已有ID
        row = conn.execute("SELECT id FROM brands WHERE name = ?", (name,)).fetchone()
        brand_id = row["id"] if row else brand_id
    conn.close()
    return brand_id


def insert_comment(data: dict) -> bool:
    """
    插入评论，返回是否为新插入（True=新评论，False=重复跳过）
    data 需包含: original_id, content, video_id, brand_id, author, like_count, posted_at, language
    可选: parent_id, depth, quality_score, filtered, filter_reason, meta_json（v2.0 质量过滤）
    """
    conn = get_db()
    comment_id = str(uuid.uuid4())
    try:
        conn.execute("""
            INSERT INTO comments
                (id, platform, original_id, video_id, brand_id, content, content_clean,
                 language, author, like_count, posted_at, source_url, sentiment_pre, analyzed,
                 parent_id, depth, quality_score, filtered, filter_reason, meta_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
        """, (
            comment_id,
            data.get("platform", "youtube"),
            data["original_id"],
            data.get("video_id"),
            data.get("brand_id"),
            data["content"],
            data.get("content_clean", data["content"]),
            data.get("language"),
            data.get("author"),
            data.get("like_count", 0),
            data.get("posted_at"),
            data.get("source_url"),
            data.get("sentiment_pre"),
            data.get("parent_id"),
            data.get("depth", 0),
            data.get("quality_score"),
            data.get("filtered", 0),
            data.get("filter_reason"),
            data.get("meta_json"),
        ))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False


def insert_analysis(comment_id: str, result: dict, model: str) -> str:
    """插入分析结果"""
    result = normalize_analysis_result(result)
    conn = get_db()
    analysis_id = str(uuid.uuid4())
    conn.execute("""
        INSERT OR REPLACE INTO analyses
            (id, comment_id, sentiment_score, pain_categories, pain_tags,
             severity, user_solution, product_match, translation_zh, summary_zh,
             confidence, llm_model, prompt_version,
             context_environment, hardware_component, user_action, pain_root_cause,
             positive_tags, emotion_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        analysis_id,
        comment_id,
        result["sentiment_score"],
        json.dumps(result.get("pain_categories", []), ensure_ascii=False),
        json.dumps(result.get("pain_tags", []), ensure_ascii=False),
        result["severity"],
        result.get("user_solution"),
        result.get("product_match"),
        result.get("translation_zh"),
        result["summary_zh"],
        result.get("confidence"),
        model,
        result.get("prompt_version"),
        result.get("context_environment"),
        result.get("hardware_component"),
        result.get("user_action"),
        result.get("pain_root_cause"),
        json.dumps(result.get("positive_tags", []), ensure_ascii=False),
        result.get("emotion_type"),
    ))
    # 标记评论已分析
    conn.execute("UPDATE comments SET analyzed = 1 WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
    return analysis_id


def get_unanalyzed_comments(limit: int = 50, brand: str | None = None) -> list[dict]:
    """
    获取未分析的评论。
    - brand 为空时：轮询分配，确保各品牌公平覆盖（避免只取最后抓取的品牌）
    - brand 指定时：只取该品牌的未分析评论
    """
    conn = get_db()

    if brand:
        # 指定品牌 — 直接取该品牌最新评论（跳过被质量过滤的评论）
        rows = conn.execute("""
            SELECT c.*, b.name as brand_name
            FROM comments c
            LEFT JOIN brands b ON c.brand_id = b.id
            WHERE c.analyzed = 0 AND b.name = ? AND c.filtered = 0
            ORDER BY c.crawled_at DESC
            LIMIT ?
        """, (brand, limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # 全部品牌 — 轮询分配（Python 层实现，兼容所有 SQLite 版本）
    rows = conn.execute("""
        SELECT c.*, b.name as brand_name
        FROM comments c
        LEFT JOIN brands b ON c.brand_id = b.id
        WHERE c.analyzed = 0 AND c.filtered = 0
        ORDER BY c.brand_id, c.crawled_at DESC
    """).fetchall()

    # 按品牌分组，保留各自顺序
    from collections import defaultdict
    by_brand: dict = defaultdict(list)
    for r in rows:
        by_brand[r["brand_id"]].append(r)

    # 轮流从每个品牌取一条，直到达到 limit
    result = []
    brand_ids = sorted(by_brand.keys())
    max_round = max(len(v) for v in by_brand.values()) if by_brand else 0
    for i in range(max_round):
        for bid in brand_ids:
            if i < len(by_brand[bid]):
                result.append(by_brand[bid][i])
                if len(result) >= limit:
                    break
        if len(result) >= limit:
            break

    conn.close()
    return [dict(r) for r in result]


def get_comment_analysis(comment_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute("""
        SELECT c.id, c.content, c.content_clean, c.language, c.platform,
               c.author, c.like_count, c.posted_at, c.source_url, c.analyzed,
               b.name AS brand_name, a.sentiment_score, a.pain_categories,
               a.pain_tags, a.severity, a.user_solution, a.product_match,
               a.translation_zh, a.summary_zh, a.confidence, a.llm_model,
               a.prompt_version, a.analyzed_at, a.human_corrected
        FROM comments c
        LEFT JOIN brands b ON c.brand_id = b.id
        LEFT JOIN analyses a ON c.id = a.comment_id
        WHERE c.id = ?
    """, (comment_id,)).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    for key in ("pain_categories", "pain_tags"):
        result[key] = _json_or_default(result.get(key), [])
    return result


def update_analysis(comment_id: str, result: dict, human_corrected: bool = True) -> bool:
    """人工修正已分析结果，仍沿用相同枚举校验。"""
    normalized = normalize_analysis_result(result)
    conn = get_db()
    cur = conn.execute("""
        UPDATE analyses SET sentiment_score = ?, pain_categories = ?, pain_tags = ?,
            severity = ?, user_solution = ?, product_match = ?, translation_zh = ?,
            summary_zh = ?, confidence = ?, human_corrected = ?, analyzed_at = datetime('now')
        WHERE comment_id = ?
    """, (
        normalized["sentiment_score"], json.dumps(normalized["pain_categories"], ensure_ascii=False),
        json.dumps(normalized["pain_tags"], ensure_ascii=False), normalized["severity"],
        normalized.get("user_solution"), normalized.get("product_match"),
        normalized.get("translation_zh", ""), normalized["summary_zh"],
        normalized.get("confidence"), 1 if human_corrected else 0, comment_id,
    ))
    if cur.rowcount:
        conn.execute("UPDATE comments SET analyzed = 1 WHERE id = ?", (comment_id,))
    conn.commit()
    conn.close()
    return bool(cur.rowcount)


def get_pain_points(
    brand: str | None = None,
    platform: str | None = None,
    min_severity: int = 1,
    limit: int = 100,
) -> list[dict]:
    """获取痛点列表（已分析的评论 + 分析结果）"""
    conn = get_db()
    query = """
        SELECT c.content, c.content_clean, c.language, c.author, c.posted_at,
               c.platform, c.like_count, c.original_id, c.source_url as comment_source_url,
               b.name as brand_name,
               v.external_id as external_video_id, v.platform as video_platform,
               v.source_url as video_source_url, v.title as video_title,
               a.sentiment_score, a.pain_categories, a.pain_tags,
               a.severity, a.user_solution, a.product_match, a.translation_zh,
               a.summary_zh, a.confidence, a.llm_model, a.prompt_version, a.analyzed_at,
               a.context_environment, a.hardware_component,
               a.user_action, a.pain_root_cause,
               a.positive_tags, a.emotion_type, a.human_corrected
        FROM comments c
        INNER JOIN analyses a ON c.id = a.comment_id
        LEFT JOIN brands b ON c.brand_id = b.id
        LEFT JOIN videos v ON c.video_id = v.id
        WHERE a.severity >= ?
    """
    params: list = [min_severity]

    if brand:
        query += " AND b.name = ?"
        params.append(brand)
    if platform:
        query += " AND c.platform = ?"
        params.append(platform)

    query += " ORDER BY a.severity DESC, a.sentiment_score ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """获取统计数据"""
    conn = get_db()
    stats = {
        "total_comments": conn.execute("SELECT COUNT(*) as c FROM comments").fetchone()["c"],
        "analyzed_comments": conn.execute("SELECT COUNT(*) as c FROM comments WHERE analyzed = 1").fetchone()["c"],
        "total_brands": conn.execute("SELECT COUNT(*) as c FROM brands").fetchone()["c"],
        "total_videos": conn.execute("SELECT COUNT(*) as c FROM videos").fetchone()["c"],
        "high_severity": conn.execute("SELECT COUNT(*) as c FROM analyses WHERE severity = 3").fetchone()["c"],
        "human_corrected": conn.execute("SELECT COUNT(*) as c FROM analyses WHERE human_corrected = 1").fetchone()["c"],
        "avg_confidence": conn.execute("SELECT AVG(confidence) as c FROM analyses WHERE confidence IS NOT NULL").fetchone()["c"],
        "filtered_comments": conn.execute("SELECT COUNT(*) as c FROM comments WHERE filtered = 1").fetchone()["c"],
    }
    # 痛点标签统计
    tag_rows = conn.execute("""
        SELECT pain_tags FROM analyses
    """).fetchall()

    tag_count: dict[str, int] = {}
    for row in tag_rows:
        tags = json.loads(row["pain_tags"]) if row["pain_tags"] else []
        for tag in tags:
            tag_count[tag] = tag_count.get(tag, 0) + 1

    stats["top_tags"] = sorted(tag_count.items(), key=lambda x: -x[1])[:10]
    if stats["avg_confidence"] is not None:
        stats["avg_confidence"] = round(float(stats["avg_confidence"]), 4)
    conn.close()
    return stats


# ==================== 第三层：结构化洞察聚合 ====================

def get_tag_distribution() -> list[dict]:
    """痛点标签分布统计（含严重度细分）"""
    conn = get_db()
    rows = conn.execute("""
        SELECT a.pain_tags, a.severity FROM analyses a
    """).fetchall()
    conn.close()

    tag_data: dict[str, dict] = {}
    for row in rows:
        tags = json.loads(row["pain_tags"]) if row["pain_tags"] else []
        for tag in tags:
            if tag not in tag_data:
                tag_data[tag] = {"tag": tag, "count": 0, "sev1": 0, "sev2": 0, "sev3": 0}
            tag_data[tag]["count"] += 1
            tag_data[tag][f"sev{row['severity']}"] += 1

    result = sorted(tag_data.values(), key=lambda x: -x["count"])
    return result


def get_brand_tag_matrix() -> dict:
    """品牌×痛点交叉矩阵（用于热力图）"""
    conn = get_db()
    rows = conn.execute("""
        SELECT b.name as brand, a.pain_tags, a.severity
        FROM analyses a
        JOIN comments c ON a.comment_id = c.id
        JOIN brands b ON c.brand_id = b.id
    """).fetchall()
    conn.close()

    # 收集所有品牌和标签
    brands_set: set = set()
    tags_set: set = set()
    matrix: dict = {}  # {brand: {tag: {count, sev3_count}}}

    for row in rows:
        brand = row["brand"] or "未知"
        brands_set.add(brand)
        tags = json.loads(row["pain_tags"]) if row["pain_tags"] else []
        for tag in tags:
            tags_set.add(tag)
            key = (brand, tag)
            if key not in matrix:
                matrix[key] = {"count": 0, "sev3": 0}
            matrix[key]["count"] += 1
            if row["severity"] == 3:
                matrix[key]["sev3"] += 1

    brands = sorted(brands_set)
    tags = sorted(tags_set)

    # 构建二维数组
    data = []
    for brand in brands:
        row_data = []
        for tag in tags:
            cell = matrix.get((brand, tag), {"count": 0, "sev3": 0})
            row_data.append(cell["count"])
        data.append(row_data)

    return {"brands": brands, "tags": tags, "data": data}


def get_model_pain_ranking() -> list[dict]:
    """型号痛点排名（高严重度痛点数 + 总痛点数）"""
    conn = get_db()
    rows = conn.execute("""
        SELECT a.product_match, a.severity, a.pain_tags
        FROM analyses a
        WHERE a.product_match IS NOT NULL AND a.product_match != ''
    """).fetchall()
    conn.close()

    model_data: dict[str, dict] = {}
    for row in rows:
        model = row["product_match"]
        if model not in model_data:
            model_data[model] = {
                "model": model, "total": 0, "sev3": 0, "sev2": 0, "sev1": 0
            }
        model_data[model]["total"] += 1
        model_data[model][f"sev{row['severity']}"] += 1

    return sorted(model_data.values(), key=lambda x: -x["sev3"])[:20]


def get_priority_matrix() -> list[dict]:
    """优先级矩阵：痛点频率 × 平均严重度（四象限）"""
    conn = get_db()
    rows = conn.execute("""
        SELECT a.pain_tags, a.severity, a.user_solution
        FROM analyses a
    """).fetchall()
    conn.close()

    tag_data: dict[str, dict] = {}
    for row in rows:
        tags = json.loads(row["pain_tags"]) if row["pain_tags"] else []
        for tag in tags:
            if tag not in tag_data:
                tag_data[tag] = {
                    "tag": tag, "count": 0, "severity_sum": 0,
                    "has_solution": 0
                }
            tag_data[tag]["count"] += 1
            tag_data[tag]["severity_sum"] += row["severity"]
            if row["user_solution"]:
                tag_data[tag]["has_solution"] += 1

    result = []
    for tag, d in tag_data.items():
        result.append({
            "tag": tag,
            "count": d["count"],
            "avg_severity": round(d["severity_sum"] / d["count"], 2),
            "has_solution": d["has_solution"],
            "score": round(d["count"] * (d["severity_sum"] / d["count"]), 2),
        })

    return sorted(result, key=lambda x: -x["score"])[:20]


def get_severity_distribution() -> dict:
    """严重度分布饼图数据"""
    conn = get_db()
    rows = conn.execute("""
        SELECT severity, COUNT(*) as cnt FROM analyses GROUP BY severity ORDER BY severity
    """).fetchall()
    conn.close()
    labels = {1: "轻微吐槽", 2: "影响体验", 3: "致命缺陷"}
    return {
        "labels": [labels.get(r["severity"], str(r["severity"])) for r in rows],
        "values": [r["cnt"] for r in rows],
    }


def get_sentiment_distribution() -> dict:
    """情感分布数据"""
    conn = get_db()
    rows = conn.execute("""
        SELECT sentiment_score, COUNT(*) as cnt
        FROM analyses GROUP BY sentiment_score ORDER BY sentiment_score
    """).fetchall()
    conn.close()
    labels = {1: "极度负面", 2: "负面", 3: "中性", 4: "正面", 5: "极度正面"}
    return {
        "labels": [labels.get(r["sentiment_score"], str(r["sentiment_score"])) for r in rows],
        "values": [r["cnt"] for r in rows],
    }


def get_emotion_distribution() -> dict:
    """情绪类型分布数据（红帽维度）。返回 keys 字段供前端颜色映射。"""
    conn = get_db()
    rows = conn.execute("""
        SELECT emotion_type, COUNT(*) as cnt
        FROM analyses
        WHERE emotion_type IS NOT NULL AND emotion_type != ''
        GROUP BY emotion_type ORDER BY cnt DESC
    """).fetchall()
    conn.close()
    labels_map = {
        "anger": "愤怒", "disappointment": "失望",
        "satisfaction": "满意", "surprise": "惊喜", "neutral": "中性",
    }
    return {
        "labels": [labels_map.get(r["emotion_type"], r["emotion_type"]) for r in rows],
        "values": [r["cnt"] for r in rows],
        "keys": [r["emotion_type"] for r in rows],
    }


def get_sev3_by_brand() -> list[dict]:
    """致命缺陷(severity=3)按品牌聚合（黑帽维度）。"""
    conn = get_db()
    rows = conn.execute("""
        SELECT COALESCE(b.name, 'N/A') as brand, COUNT(*) as cnt
        FROM analyses a
        JOIN comments c ON a.comment_id = c.id
        LEFT JOIN brands b ON c.brand_id = b.id
        WHERE a.severity = 3
        GROUP BY brand ORDER BY cnt DESC
    """).fetchall()
    conn.close()
    return [{"brand": r["brand"], "count": r["cnt"]} for r in rows]


def get_solution_tags() -> list[dict]:
    """含用户建议的评论中痛点标签频率 Top 10（绿帽维度）。"""
    import json as _json
    conn = get_db()
    rows = conn.execute("""
        SELECT a.pain_tags FROM analyses a
        WHERE a.user_solution IS NOT NULL AND a.user_solution != ''
    """).fetchall()
    conn.close()
    tag_count: dict[str, int] = {}
    for row in rows:
        tags = _json.loads(row["pain_tags"]) if row["pain_tags"] else []
        for tag in tags:
            tag_count[tag] = tag_count.get(tag, 0) + 1
    result = [{"tag": t, "count": c} for t, c in sorted(tag_count.items(), key=lambda x: -x[1])]
    return result[:10]


def get_analysis_progress() -> dict:
    """AI 分析进度统计（蓝帽维度 + 进度卡片）。"""
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    analyzed = conn.execute("SELECT COUNT(*) FROM comments WHERE analyzed = 1").fetchone()[0]
    unanalyzed = total - analyzed
    old_analyses = conn.execute(
        "SELECT COUNT(*) FROM analyses WHERE emotion_type IS NULL OR emotion_type = ''"
    ).fetchone()[0]
    conn.close()
    return {
        "total": total,
        "analyzed": analyzed,
        "unanalyzed": unanalyzed,
        "old_analyses": old_analyses,
        "coverage": round(analyzed / total * 100, 1) if total > 0 else 0,
    }


def get_field_fill_rates() -> dict:
    """结构化字段填充率（蓝帽维度）。"""
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
    if total == 0:
        conn.close()
        return {"total": 0, "fields": []}
    fields = [
        ("pain_tags", "痛点标签"),
        ("severity", "严重度"),
        ("sentiment_score", "情感分值"),
        ("summary_zh", "中文摘要"),
        ("emotion_type", "情绪类型"),
        ("context_environment", "场景环境"),
        ("hardware_component", "硬件元器件"),
        ("user_action", "用户行为"),
        ("pain_root_cause", "根因分析"),
        ("positive_tags", "正面标签"),
        ("user_solution", "用户建议"),
        ("confidence", "置信度"),
    ]
    result = []
    for key, label in fields:
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM analyses WHERE {key} IS NOT NULL AND CAST({key} AS TEXT) != ''"
        ).fetchone()[0]
        result.append({"key": key, "label": label, "filled": cnt, "rate": round(cnt / total * 100, 1)})
    conn.close()
    return {"total": total, "fields": result}


def reset_analyses_for_reanalysis(brand: str | None = None) -> int:
    """重置旧分析的 analyzed 标记并删除旧分析记录，使其可被新版 Prompt 重新分析。
    返回重置的评论数量。"""
    conn = get_db()
    if brand:
        rows = conn.execute("""
            SELECT c.id FROM comments c
            JOIN brands b ON c.brand_id = b.id
            WHERE c.analyzed = 1 AND b.name = ?
        """, (brand,)).fetchall()
        comment_ids = [r["id"] for r in rows]
        if comment_ids:
            placeholders = ",".join("?" * len(comment_ids))
            conn.execute(f"DELETE FROM analyses WHERE comment_id IN ({placeholders})", comment_ids)
            conn.execute(f"UPDATE comments SET analyzed = 0 WHERE id IN ({placeholders})", comment_ids)
    else:
        count = conn.execute("SELECT COUNT(*) FROM comments WHERE analyzed = 1").fetchone()[0]
        conn.execute("DELETE FROM analyses")
        conn.execute("UPDATE comments SET analyzed = 0")
        conn.commit()
        conn.close()
        return count
    conn.commit()
    conn.close()
    return len(comment_ids)


def get_positive_tags_distribution() -> list[dict]:
    """正面反馈标签排行（黄帽维度）"""
    conn = get_db()
    rows = conn.execute("SELECT positive_tags FROM analyses WHERE positive_tags IS NOT NULL AND positive_tags != ''").fetchall()
    conn.close()
    tag_count: dict[str, int] = {}
    for row in rows:
        tags = json.loads(row["positive_tags"]) if row["positive_tags"] else []
        for tag in tags:
            tag_count[tag] = tag_count.get(tag, 0) + 1
    result = [{"tag": t, "count": c} for t, c in sorted(tag_count.items(), key=lambda x: -x[1])]
    return result[:20]


def get_user_solutions() -> list[dict]:
    """用户改良方案汇总（绿帽维度）"""
    conn = get_db()
    rows = conn.execute("""
        SELECT c.content_clean, b.name as brand, a.pain_tags, a.user_solution,
               a.product_match, a.summary_zh
        FROM analyses a
        JOIN comments c ON a.comment_id = c.id
        LEFT JOIN brands b ON c.brand_id = b.id
        WHERE a.user_solution IS NOT NULL AND a.user_solution != ''
        ORDER BY a.analyzed_at DESC LIMIT 50
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["pain_tags"] = json.loads(d.get("pain_tags") or "[]")
        result.append(d)
    return result


# ==================== 第四层：AI 改良建议数据源 ====================

def get_insights_summary() -> dict:
    """汇总痛点数据供 AI 生成改良建议（后端聚合，前端传给LLM）"""
    conn = get_db()
    # Top 痛点（频率+严重度）
    tag_dist = get_tag_distribution()
    top_pains = [t for t in tag_dist if t["count"] >= 3][:10]

    # 高严重度痛点原文样本
    high_sev_rows = conn.execute("""
        SELECT c.content_clean, b.name as brand, a.pain_tags, a.summary_zh, a.product_match
        FROM analyses a
        JOIN comments c ON a.comment_id = c.id
        LEFT JOIN brands b ON c.brand_id = b.id
        WHERE a.severity = 3
        ORDER BY a.analyzed_at DESC LIMIT 20
    """).fetchall()

    # 用户方案
    solution_rows = conn.execute("""
        SELECT c.content_clean, b.name as brand, a.pain_tags, a.user_solution, a.product_match
        FROM analyses a
        JOIN comments c ON a.comment_id = c.id
        LEFT JOIN brands b ON c.brand_id = b.id
        WHERE a.user_solution IS NOT NULL AND a.user_solution != ''
        ORDER BY a.analyzed_at DESC LIMIT 30
    """).fetchall()

    # 品牌痛点对比
    brand_rows = conn.execute("""
        SELECT b.name as brand,
               COUNT(a.id) as total,
               SUM(CASE WHEN a.severity=3 THEN 1 ELSE 0 END) as sev3,
               AVG(a.severity) as avg_sev
        FROM analyses a
        JOIN comments c ON a.comment_id = c.id
        JOIN brands b ON c.brand_id = b.id
        GROUP BY b.name ORDER BY sev3 DESC
    """).fetchall()
    conn.close()

    return {
        "top_pains": top_pains,
        "high_severity_samples": [dict(r) for r in high_sev_rows],
        "user_solutions": [dict(r) for r in solution_rows],
        "brand_comparison": [{
            "brand": r["brand"],
            "total": r["total"],
            "sev3": r["sev3"],
            "avg_sev": round(r["avg_sev"], 2) if r["avg_sev"] else 0,
        } for r in brand_rows],
    }


# ==================== 后台任务 ====================

def get_active_job(kind: str, params: dict | None = None) -> dict | None:
    """Return a queued/running job with the same canonical parameters."""
    canonical = json.dumps(params or {}, ensure_ascii=False, sort_keys=True)
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE kind = ? AND status IN ('queued', 'running') ORDER BY created_at DESC",
        (kind,),
    ).fetchall()
    conn.close()
    for row in rows:
        if json.dumps(_json_or_default(row["params"], {}), ensure_ascii=False, sort_keys=True) == canonical:
            return _job_row(row)
    return None


def create_job(kind: str, params: dict | None = None) -> str:
    """创建一个可轮询的后台任务，返回任务 ID。"""
    job_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO jobs (id, kind, status, params) VALUES (?, ?, 'queued', ?)",
        (job_id, kind, json.dumps(params or {}, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return job_id


def request_job_cancel(job_id: str) -> dict | None:
    """Request cooperative cancellation; queued jobs are cancelled immediately."""
    conn = get_db()
    conn.execute("""
        UPDATE jobs
        SET cancel_requested = 1,
            status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
            message = CASE WHEN status = 'queued' THEN '任务已取消' ELSE '正在取消任务' END,
            completed_at = CASE WHEN status = 'queued' THEN datetime('now') ELSE completed_at END
        WHERE id = ? AND status IN ('queued', 'running')
    """, (job_id,))
    conn.commit()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return _job_row(row)


def is_job_cancel_requested(job_id: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT cancel_requested, status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    conn.close()
    return bool(row and (row["cancel_requested"] or row["status"] == "cancelled"))


def update_job(job_id: str, **fields):
    """更新任务状态；字段名使用白名单，避免动态 SQL 注入。"""
    allowed = {
        "status", "progress_current", "progress_total", "message", "result_json",
        "error", "started_at", "completed_at",
    }
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn = get_db()
    conn.execute(
        f"UPDATE jobs SET {assignments} WHERE id = ?",
        (*values.values(), job_id),
    )
    conn.commit()
    conn.close()


def _job_row(row) -> dict | None:
    if not row:
        return None
    result = dict(row)
    result["params"] = _json_or_default(result.get("params"), {})
    result["result"] = _json_or_default(result.pop("result_json", None), None)
    return result


def get_job(job_id: str) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return _job_row(row)


def get_recent_jobs(limit: int = 20) -> list[dict]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 100)),)
    ).fetchall()
    conn.close()
    return [_job_row(row) for row in rows]




def get_setting(key: str, default: str = "") -> str:
    """读取单个配置项"""
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return _unprotect_setting(key, row["value"]) if row else default


def set_setting(key: str, value: str):
    """写入单个配置项"""
    conn = get_db()
    conn.execute("""
        INSERT INTO settings (key, value, updated_at) VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = datetime('now')
    """, (key, value, value))
    conn.commit()
    conn.close()


def get_all_settings() -> dict:
    """读取所有配置项，返回 dict"""
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {row["key"]: _unprotect_setting(row["key"], row["value"]) for row in rows}


def get_llm_config() -> dict:
    """获取当前 LLM 配置"""
    s = get_all_settings()
    provider = s.get("llm_provider", "")
    return {
        "provider": provider,
        "api_keys": {
            "gemini": s.get("gemini_api_key", ""),
            "deepseek": s.get("deepseek_api_key", ""),
            "glm": s.get("glm_api_key", ""),
            "kimi": s.get("kimi_api_key", ""),
            "qwen": s.get("qwen_api_key", ""),
        },
        "models": {
            "gemini": s.get("gemini_model", ""),
            "deepseek": s.get("deepseek_model", ""),
            "glm": s.get("glm_model", ""),
            "kimi": s.get("kimi_model", ""),
            "qwen": s.get("qwen_model", ""),
        },
    }


def save_llm_config(config: dict):
    """保存 LLM 配置"""
    if "provider" in config:
        set_setting("llm_provider", config["provider"])
    if "api_keys" in config:
        for provider, key in config["api_keys"].items():
            set_setting(f"{provider}_api_key", _protect_setting(f"{provider}_api_key", key))
    if "models" in config:
        for provider, model in config["models"].items():
            set_setting(f"{provider}_model", model)




# ==================== Reddit OAuth ?? ====================

def get_reddit_config() -> dict:
    """?? Reddit OAuth ????????"""
    s = get_all_settings()
    client_id = s.get("reddit_client_id", "")
    client_secret = s.get("reddit_client_secret", "")
    return {
        "client_id": client_id,
        "client_secret": (client_secret[:2] + "***" + client_secret[-2:]) if len(client_secret) > 4 else ("***" if client_secret else ""),
        "username": s.get("reddit_username", ""),
        "password": ("***" if s.get("reddit_password", "") else ""),
        "configured": bool(client_id and client_secret),
    }


def save_reddit_config(config: dict):
    """?? Reddit OAuth ??"""
    existing = get_all_settings()
    if "client_id" in config:
        set_setting("reddit_client_id", config["client_id"])
    if "client_secret" in config:
        val = config["client_secret"]
        if not val or "***" not in val:
            set_setting("reddit_client_secret", _protect_setting("reddit_client_secret", val))
    if "username" in config:
        set_setting("reddit_username", config["username"])
    if "password" in config:
        val = config["password"]
        if not val or "***" not in val:
            set_setting("reddit_password", _protect_setting("reddit_password", val))


# ==================== Reddit Cookie 配置（方案B）====================

def get_reddit_cookie_config() -> dict:
    """读取 Reddit Cookie 配置（cookies 脱敏）"""
    s = get_all_settings()
    cookies = s.get("reddit_cookies", "")
    cached = s.get("reddit_cached_cookies", "")
    method = s.get("reddit_cookie_method", "browser")
    return {
        "method": method,
        "browser": s.get("reddit_browser", "chrome"),
        "cookies": ("***" if cookies else ""),
        "has_cached": bool(cached),
        "configured": bool(cookies) if method == "manual" else bool(cached),
    }


def save_reddit_cookie_config(config: dict):
    """保存 Reddit Cookie 配置"""
    if "method" in config:
        set_setting("reddit_cookie_method", config["method"])
    if "browser" in config:
        set_setting("reddit_browser", config["browser"])
    if "cookies" in config:
        val = config["cookies"]
        if not val or "***" not in val:
            set_setting("reddit_cookies", _protect_setting("reddit_cookies", val))


# ==================== Instagram 账号配置 ====================

def get_instagram_config() -> dict:
    """读取 Instagram 账号配置（密码脱敏）"""
    s = get_all_settings()
    username = s.get("instagram_username", "")
    password = s.get("instagram_password", "")
    return {
        "username": username,
        "password": ("***" if password else ""),
        "configured": bool(username and password),
    }


def save_instagram_config(config: dict):
    """保存 Instagram 账号配置"""
    if "username" in config:
        set_setting("instagram_username", config["username"])
    if "password" in config:
        val = config["password"]
        if not val or "***" not in val:
            set_setting("instagram_password", _protect_setting("instagram_password", val))


# ==================== Facebook Cookies 配置 ====================

def get_facebook_config() -> dict:
    """读取 Facebook cookies 配置（脱敏）"""
    s = get_all_settings()
    cookies = s.get("facebook_cookies", "")
    return {
        "cookies": ("***" if cookies else ""),
        "configured": bool(cookies),
    }


def save_facebook_config(config: dict):
    """保存 Facebook cookies 配置"""
    if "cookies" in config:
        val = config["cookies"]
        if not val or "***" not in val:
            set_setting("facebook_cookies", _protect_setting("facebook_cookies", val))


# ==================== 品牌管理 ====================

def add_brand(name: str, search_keyword: str, brand_type: str = "competitor") -> dict:
    """添加品牌，返回 {id, name, search_keyword}"""
    brand_type = (brand_type or "competitor").strip().lower()
    if brand_type not in ALLOWED_BRAND_TYPES:
        raise ValueError("品牌类型必须是 competitor、own 或 benchmark")
    conn = get_db()
    brand_id = str(uuid.uuid4())
    try:
        conn.execute(
            "INSERT INTO brands (id, name, search_keyword, brand_type) VALUES (?, ?, ?, ?)",
            (brand_id, name, search_keyword, brand_type)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise ValueError(f"品牌 '{name}' 已存在")
    conn.close()
    return {"id": brand_id, "name": name, "search_keyword": search_keyword, "brand_type": brand_type}


def update_brand(brand_id: str, name: str, search_keyword: str, brand_type: str = "competitor") -> bool:
    """修改品牌信息"""
    brand_type = (brand_type or "competitor").strip().lower()
    if brand_type not in ALLOWED_BRAND_TYPES:
        raise ValueError("品牌类型必须是 competitor、own 或 benchmark")
    conn = get_db()
    try:
        conn.execute(
            "UPDATE brands SET name = ?, search_keyword = ?, brand_type = ? WHERE id = ?",
            (name, search_keyword, brand_type, brand_id)
        )
        conn.commit()
        changed = conn.total_changes > 0
    except sqlite3.IntegrityError:
        conn.close()
        raise ValueError(f"品牌名 '{name}' 已被占用")
    conn.close()
    return changed


def delete_brand(brand_id: str):
    """删除品牌及其关联数据"""
    conn = get_db()
    # 先删关联的分析结果
    conn.execute("""
        DELETE FROM analyses WHERE comment_id IN (
            SELECT id FROM comments WHERE brand_id = ?
        )
    """, (brand_id,))
    # 删评论
    conn.execute("DELETE FROM comments WHERE brand_id = ?", (brand_id,))
    # 删视频
    conn.execute("DELETE FROM videos WHERE brand_id = ?", (brand_id,))
    # 删产品
    conn.execute("DELETE FROM products WHERE brand_id = ?", (brand_id,))
    # 删品牌
    conn.execute("DELETE FROM brands WHERE id = ?", (brand_id,))
    conn.commit()
    conn.close()


def get_all_brands() -> list[dict]:
    """获取所有品牌"""
    conn = get_db()
    rows = conn.execute("""
        SELECT b.id, b.name, b.search_keyword, b.brand_type, b.created_at,
               (SELECT COUNT(*) FROM comments WHERE brand_id = b.id) as comment_count,
               (SELECT COUNT(*) FROM analyses a
                JOIN comments c ON a.comment_id = c.id
                WHERE c.brand_id = b.id) as analyzed_count
        FROM brands b ORDER BY b.name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def init_default_brands():
    """初始化默认竞品品牌"""
    competitors = [
        ("Blackview", "Blackview rugged phone review", "competitor"),
        ("Ulefone", "Ulefone Armor review", "competitor"),
        ("Doogee", "Doogee rugged phone review", "competitor"),
        ("Oukitel", "Oukitel rugged phone review", "competitor"),
        ("Unihertz", "Unihertz rugged phone review", "competitor"),
        ("FOSSiBOT", "FOSSiBOT rugged phone review", "competitor"),
        ("Oscal", "Oscal rugged phone review", "competitor"),
        ("HOTWAV", "HOTWAV rugged phone review", "competitor"),
        ("RugOne", "RugOne rugged phone review", "own"),
    ]
    for name, keyword, brand_type in competitors:
        insert_brand(name, keyword, brand_type)


# ─── 竞品规格 CRUD ───────────────────────────────────────────

def add_spec(brand_id: str, spec_category: str, spec_key: str, spec_value: str,
             spec_unit: str = "", source_url: str = "", model: str = "") -> str:
    """录入一条竞品硬件规格"""
    conn = get_db()
    spec_id = str(uuid.uuid4())
    conn.execute("""
        INSERT INTO competitor_specs (id, brand_id, model, spec_category, spec_key, spec_value, spec_unit, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (spec_id, brand_id, model, spec_category, spec_key, spec_value, spec_unit, source_url))
    conn.commit()
    conn.close()
    return spec_id


def get_specs(brand_id: str | None = None, category: str | None = None, key: str | None = None) -> list[dict]:
    """获取竞品规格列表，支持按品牌/类别/键名筛选"""
    conn = get_db()
    sql = """
        SELECT cs.*, b.name as brand_name
        FROM competitor_specs cs
        LEFT JOIN brands b ON cs.brand_id = b.id
        WHERE 1=1
    """
    params: list = []
    if brand_id:
        sql += " AND cs.brand_id = ?"
        params.append(brand_id)
    if category:
        sql += " AND cs.spec_category = ?"
        params.append(category)
    if key:
        sql += " AND cs.spec_key = ?"
        params.append(key)
    sql += " ORDER BY cs.spec_category, cs.spec_key"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_spec(spec_id: str, spec_category: str, spec_key: str, spec_value: str,
                spec_unit: str = "", source_url: str = "", model: str = "") -> bool:
    """修改竞品规格"""
    conn = get_db()
    cursor = conn.execute("""
        UPDATE competitor_specs
        SET spec_category = ?, spec_key = ?, spec_value = ?, spec_unit = ?, source_url = ?, model = ?
        WHERE id = ?
    """, (spec_category, spec_key, spec_value, spec_unit, source_url, model, spec_id))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def delete_spec(spec_id: str):
    """删除竞品规格"""
    conn = get_db()
    conn.execute("DELETE FROM competitor_specs WHERE id = ?", (spec_id,))
    conn.commit()
    conn.close()


def get_spec_regression(spec_key: str, pain_tag: str) -> dict:
    """Spec-Pain 回归分析：物理参数 × 负面声量占比"""
    conn = get_db()
    # 获取所有竞品该参数的规格值
    specs = conn.execute("""
        SELECT cs.brand_id, cs.model, cs.spec_value, cs.spec_unit, b.name as brand_name
        FROM competitor_specs cs
        LEFT JOIN brands b ON cs.brand_id = b.id
        WHERE cs.spec_key = ?
        ORDER BY cs.spec_value
    """, (spec_key,)).fetchall()

    if len(specs) < 3:
        conn.close()
        return {"error": "数据量不足以拟合回归曲线，请补充至少 3 个竞品规格", "data": []}

    data_points = []
    for spec in specs:
        spec_val = spec["spec_value"]
        try:
            x_val = float(spec_val)
        except (ValueError, TypeError):
            continue

        # 统计该品牌该痛点标签的评论中负面占比
        stats = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN a.sentiment_score <= 2 THEN 1 ELSE 0 END) as negative
            FROM analyses a
            JOIN comments c ON a.comment_id = c.id
            WHERE c.brand_id = ?
              AND a.pain_tags LIKE ?
        """, (spec["brand_id"], f'%"{pain_tag}"%')).fetchone()

        total = stats["total"] or 0
        negative = stats["negative"] or 0
        neg_ratio = (negative / total * 100) if total > 0 else 0

        data_points.append({
            "brand": spec["brand_name"],
            "model": spec["model"] or "",
            "x": x_val,
            "y": round(neg_ratio, 1),
            "total_comments": total,
            "negative_comments": negative,
            "unit": spec["spec_unit"] or "",
        })

    conn.close()

    if len(data_points) < 3:
        return {"error": "有效数值点不足 3 个，无法拟合", "data": data_points}

    # 简单线性回归找拐点：计算拐点为负面占比增速最快的 x 值
    data_points.sort(key=lambda d: d["x"])
    max_jump = 0
    threshold = None
    for i in range(1, len(data_points)):
        jump = data_points[i]["y"] - data_points[i - 1]["y"]
        if jump > max_jump:
            max_jump = jump
            threshold = data_points[i]["x"]

    return {
        "spec_key": spec_key,
        "pain_tag": pain_tag,
        "threshold": threshold,
        "data": data_points,
    }


# ─── 四元组查询 ────────────────────────────────────────────

def get_analysis_with_tuple(comment_id: str) -> dict | None:
    """获取分析结果（含四元组字段）"""
    conn = get_db()
    row = conn.execute("""
        SELECT a.*, c.content, c.content_clean, c.platform, c.source_url,
               c.author, c.like_count, c.posted_at, b.name as brand_name
        FROM analyses a
        JOIN comments c ON a.comment_id = c.id
        LEFT JOIN brands b ON c.brand_id = b.id
        WHERE a.comment_id = ?
    """, (comment_id,)).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["pain_categories"] = json.loads(result.get("pain_categories") or "[]")
    result["pain_tags"] = json.loads(result.get("pain_tags") or "[]")
    result["positive_tags"] = json.loads(result.get("positive_tags") or "[]")
    return result


def get_top_pain_comments(pain_tag: str, brand: str | None = None,
                         min_severity: int = 2, limit: int = 5) -> list[dict]:
    """获取指定痛点标签下最具代表性的评论（按点赞数+严重度排序）"""
    conn = get_db()
    sql = """
        SELECT c.id, c.content, c.content_clean, c.platform, c.source_url,
               c.author, c.like_count, c.posted_at, b.name as brand_name,
               v.title as video_title, v.source_url as video_source_url,
               a.sentiment_score, a.pain_tags, a.severity, a.user_solution,
               a.product_match, a.summary_zh, a.translation_zh,
               a.context_environment, a.hardware_component,
               a.user_action, a.pain_root_cause,
               a.positive_tags, a.emotion_type
        FROM comments c
        INNER JOIN analyses a ON c.id = a.comment_id
        LEFT JOIN brands b ON c.brand_id = b.id
        LEFT JOIN videos v ON c.video_id = v.id
        WHERE a.pain_tags LIKE ? AND a.severity >= ?
    """
    params: list = [f'%"{pain_tag}"%', min_severity]
    if brand:
        sql += " AND b.name = ?"
        params.append(brand)
    sql += " ORDER BY a.severity DESC, c.like_count DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["pain_tags"] = json.loads(d.get("pain_tags") or "[]")
        d["positive_tags"] = json.loads(d.get("positive_tags") or "[]")
        result.append(d)
    return result


# ─── Gold Standard 质量控制 ────────────────────────────────────

def add_gold_standard(comment_id: str, expected_tags: str = "",
                      expected_severity: int | None = None,
                      expected_sentiment: int | None = None,
                      expected_four_tuple: str = "") -> str:
    """录入一条 Gold Standard 人工标注"""
    conn = get_db()
    gs_id = str(uuid.uuid4())
    conn.execute("""
        INSERT OR REPLACE INTO gold_standard
            (id, comment_id, expected_tags, expected_severity, expected_sentiment, expected_four_tuple)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (gs_id, comment_id, expected_tags, expected_severity, expected_sentiment, expected_four_tuple))
    conn.commit()
    conn.close()
    return gs_id


def get_gold_standards() -> list[dict]:
    """获取所有 Gold Standard 样本"""
    conn = get_db()
    rows = conn.execute("""
        SELECT gs.*, c.content_clean, c.platform, b.name as brand_name
        FROM gold_standard gs
        JOIN comments c ON gs.comment_id = c.id
        LEFT JOIN brands b ON c.brand_id = b.id
        ORDER BY gs.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_gold_standard(gs_id: str):
    conn = get_db()
    conn.execute("DELETE FROM gold_standard WHERE id = ?", (gs_id,))
    conn.commit()
    conn.close()


def get_gold_standard_report() -> dict:
    """Gold Standard 跑分：比对 AI 输出与人工标注"""
    conn = get_db()
    rows = conn.execute("""
        SELECT gs.expected_tags, gs.expected_severity, gs.expected_sentiment,
               a.pain_tags, a.severity, a.sentiment_score
        FROM gold_standard gs
        JOIN analyses a ON gs.comment_id = a.comment_id
    """).fetchall()
    conn.close()

    total = len(rows)
    if total == 0:
        return {"error": "Gold Standard 样本为空，请先录入人工标注样本", "total": 0}

    tag_match = 0
    severity_match = 0
    sentiment_match = 0
    for r in rows:
        expected_tags_set = set(json.loads(r["expected_tags"]) if r["expected_tags"] else [])
        actual_tags_set = set(json.loads(r["pain_tags"]) if r["pain_tags"] else [])
        if expected_tags_set and expected_tags_set == actual_tags_set:
            tag_match += 1
        elif expected_tags_set and expected_tags_set.issubset(actual_tags_set):
            tag_match += 1
        if r["expected_severity"] is not None and r["expected_severity"] == r["severity"]:
            severity_match += 1
        if r["expected_sentiment"] is not None:
            exp_dir = "neg" if r["expected_sentiment"] <= 2 else ("pos" if r["expected_sentiment"] >= 4 else "neu")
            act_dir = "neg" if r["sentiment_score"] <= 2 else ("pos" if r["sentiment_score"] >= 4 else "neu")
            if exp_dir == act_dir:
                sentiment_match += 1

    return {
        "total": total,
        "tag_accuracy": round(tag_match / total * 100, 1),
        "severity_accuracy": round(severity_match / total * 100, 1),
        "sentiment_accuracy": round(sentiment_match / total * 100, 1),
        "tag_match": tag_match,
        "severity_match": severity_match,
        "sentiment_match": sentiment_match,
    }


# ==================== 聚类（v2.0 Phase 2）====================

def get_clusterable_comments() -> list[dict]:
    """返回可参与聚类的评论：通过质量过滤且有清洗后文本。"""
    conn = get_db()
    rows = conn.execute("""
        SELECT c.id, c.content_clean, c.platform, c.quality_score, b.name AS brand_name
        FROM comments c
        LEFT JOIN brands b ON c.brand_id = b.id
        WHERE c.filtered = 0 AND c.content_clean IS NOT NULL AND c.content_clean != ''
        ORDER BY c.crawled_at
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_embeddings(model_version: str, items: list[tuple[str, bytes, int]]) -> int:
    """批量保存评论向量。items: [(comment_id, float32 向量 bytes, dim)]，返回新增数。"""
    if not items:
        return 0
    conn = get_db()
    conn.executemany(
        """INSERT INTO embeddings (comment_id, model_version, dim, vector)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(comment_id, model_version) DO UPDATE SET vector = excluded.vector""",
        [(cid, model_version, dim, vec) for cid, vec, dim in items],
    )
    conn.commit()
    conn.close()
    return len(items)


def load_embedding_map(model_version: str) -> dict[str, bytes]:
    """加载某模型版本的全部缓存向量 {comment_id: bytes}。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT comment_id, vector FROM embeddings WHERE model_version = ?",
        (model_version,),
    ).fetchall()
    conn.close()
    return {r["comment_id"]: r["vector"] for r in rows}


def get_embedded_comment_ids(model_version: str) -> set[str]:
    conn = get_db()
    rows = conn.execute(
        "SELECT comment_id FROM embeddings WHERE model_version = ?", (model_version,)
    ).fetchall()
    conn.close()
    return {r["comment_id"] for r in rows}


def save_cluster(cluster: dict) -> str:
    """写入一个簇（含统计与命名），返回簇 id。"""
    cluster_id = cluster.get("id") or str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        """INSERT INTO clusters (id, model_version, topic_name, topic_name_en, description,
                                 keywords, comment_count, representative_comment_ids,
                                 avg_severity, avg_sentiment)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
             topic_name = excluded.topic_name,
             topic_name_en = excluded.topic_name_en,
             description = excluded.description,
             keywords = excluded.keywords,
             comment_count = excluded.comment_count,
             representative_comment_ids = excluded.representative_comment_ids,
             avg_severity = excluded.avg_severity,
             avg_sentiment = excluded.avg_sentiment""",
        (
            cluster_id, cluster.get("model_version"), cluster.get("topic_name"),
            cluster.get("topic_name_en"), cluster.get("description"),
            json.dumps(cluster.get("keywords") or [], ensure_ascii=False),
            cluster.get("comment_count", 0),
            json.dumps(cluster.get("representative_comment_ids", []), ensure_ascii=False),
            cluster.get("avg_severity"), cluster.get("avg_sentiment"),
        ),
    )
    conn.commit()
    conn.close()
    return cluster_id


def set_active_cluster_version(version: str):
    set_setting("active_cluster_version", version)


def get_active_cluster_version() -> str:
    return get_setting("active_cluster_version", "")


def get_clusters(version: str | None = None) -> list[dict]:
    """按版本列出簇（默认当前活跃版本），按规模降序。"""
    version = version or get_active_cluster_version()
    if not version:
        return []
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM clusters WHERE model_version = ? ORDER BY comment_count DESC",
        (version,),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        c = dict(r)
        c["representative_comment_ids"] = _json_or_default(c.get("representative_comment_ids"), [])
        c["keywords"] = _json_or_default(c.get("keywords"), [])
        result.append(c)
    return result


def assign_comments_to_clusters(assignments: dict[str, str]):
    """批量更新评论归属：{comment_id: cluster_id}。cluster_id 为 None 表示移出簇。"""
    if not assignments:
        return
    conn = get_db()
    conn.executemany(
        "UPDATE comments SET cluster_id = ? WHERE id = ?",
        [(cid, com) for com, cid in assignments.items()],
    )
    conn.commit()
    conn.close()


def clear_cluster_assignments():
    """全量重聚类前清空所有评论的簇归属。"""
    conn = get_db()
    conn.execute("UPDATE comments SET cluster_id = NULL")
    conn.commit()
    conn.close()


def _pick_translation(cached: str | None, analyzed_full: str | None, summary: str | None) -> tuple[str, str]:
    """译文优先级：缓存表全文翻译 > 分析时全文翻译 > 中文摘要兜底。
    返回 (译文, 来源标记 cache/analysis/summary/空)。"""
    cached = (cached or "").strip()
    analyzed_full = (analyzed_full or "").strip()
    summary = (summary or "").strip()
    if cached:
        return cached, "cache"
    if analyzed_full:
        return analyzed_full, "analysis"
    if summary:
        return summary, "summary"
    return "", ""


def get_cluster_comments(cluster_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    """簇内评论，质量分降序，带分析结果、品牌名与最优中文译文。"""
    conn = get_db()
    rows = conn.execute("""
        SELECT c.id, c.platform, c.content_clean, c.quality_score, c.like_count,
               c.source_url, b.name AS brand_name,
               a.severity, a.sentiment_score, a.summary_zh, a.translation_zh, a.pain_tags
        FROM comments c
        LEFT JOIN brands b ON c.brand_id = b.id
        LEFT JOIN analyses a ON a.comment_id = c.id
        WHERE c.cluster_id = ?
        ORDER BY c.quality_score DESC, c.like_count DESC
        LIMIT ? OFFSET ?
    """, (cluster_id, limit, offset)).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    cached_map = get_comment_translations([r["id"] for r in result])
    for r in result:
        r["translation_zh"], r["translation_source"] = _pick_translation(
            cached_map.get(r["id"]), r.get("translation_zh"), r.get("summary_zh")
        )
    return result


def get_cluster_stats_for(comment_ids: list[str]) -> dict:
    """按簇内评论聚合分析统计：平均严重度 / 平均情感 / 平台与品牌分布。"""
    if not comment_ids:
        return {"avg_severity": None, "avg_sentiment": None,
                "platforms": {}, "brands": {}, "analyzed": 0}
    placeholders = ",".join("?" for _ in comment_ids)
    conn = get_db()
    rows = conn.execute(f"""
        SELECT c.platform, b.name AS brand_name, a.severity, a.sentiment_score
        FROM comments c
        LEFT JOIN brands b ON c.brand_id = b.id
        LEFT JOIN analyses a ON a.comment_id = c.id
        WHERE c.id IN ({placeholders})
    """, comment_ids).fetchall()
    conn.close()
    sev = [r["severity"] for r in rows if r["severity"] is not None]
    sent = [r["sentiment_score"] for r in rows if r["sentiment_score"] is not None]
    platforms: dict[str, int] = {}
    brands: dict[str, int] = {}
    for r in rows:
        if r["platform"]:
            platforms[r["platform"]] = platforms.get(r["platform"], 0) + 1
        if r["brand_name"]:
            brands[r["brand_name"]] = brands.get(r["brand_name"], 0) + 1
    return {
        "avg_severity": round(sum(sev) / len(sev), 2) if sev else None,
        "avg_sentiment": round(sum(sent) / len(sent), 2) if sent else None,
        "platforms": platforms,
        "brands": brands,
        "analyzed": len(sev),
    }


def get_clustered_comment_count() -> int:
    """当前已归入簇（非噪声）的评论数。"""
    conn = get_db()
    n = conn.execute(
        "SELECT COUNT(*) FROM comments WHERE cluster_id IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    return n


# ==================== 建议报告持久化（v2.0 Phase 3）====================

def save_report(report: dict) -> str:
    """写入一版报告，返回报告 id。"""
    report_id = report.get("id") or str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        """INSERT INTO reports (id, title, params, content_md, suggestion_ids, model, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            report_id, report.get("title") or "痛点改良建议报告",
            json.dumps(report.get("params") or {}, ensure_ascii=False),
            report.get("content_md") or "",
            json.dumps(report.get("suggestion_ids") or [], ensure_ascii=False),
            report.get("model") or "",
            report.get("created_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()
    return report_id


def save_suggestion(suggestion: dict) -> str:
    """写入一条建议对象（证据链 JSON 化存储），返回建议 id。"""
    sid = suggestion.get("id") or str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        """INSERT INTO suggestions (id, report_id, cluster_id, title, priority_score,
                                    priority_factors, evidence_comment_ids, evidence_quotes,
                                    affected_brands, spec_hint, effort, opportunity, detail_md)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            sid, suggestion.get("report_id"), suggestion.get("cluster_id"),
            suggestion.get("title") or "", suggestion.get("priority_score"),
            suggestion.get("priority_factors") or "",
            json.dumps(suggestion.get("evidence_comment_ids") or [], ensure_ascii=False),
            json.dumps(suggestion.get("evidence_quotes") or [], ensure_ascii=False),
            json.dumps(suggestion.get("affected_brands") or [], ensure_ascii=False),
            suggestion.get("spec_hint") or "", suggestion.get("effort") or "M",
            suggestion.get("opportunity") or "", suggestion.get("detail_md") or "",
        ),
    )
    conn.commit()
    conn.close()
    # 回填报告的建议 id 列表
    if suggestion.get("report_id"):
        conn = get_db()
        row = conn.execute(
            "SELECT suggestion_ids FROM reports WHERE id = ?", (suggestion["report_id"],)
        ).fetchone()
        ids = json.loads(row["suggestion_ids"]) if row and row["suggestion_ids"] else []
        ids.append(sid)
        conn.execute(
            "UPDATE reports SET suggestion_ids = ? WHERE id = ?",
            (json.dumps(ids, ensure_ascii=False), suggestion["report_id"]),
        )
        conn.commit()
        conn.close()
    return sid


def _suggestion_row_to_dict(row) -> dict:
    s = dict(row)
    for field in ("evidence_comment_ids", "evidence_quotes", "affected_brands"):
        try:
            s[field] = json.loads(s.get(field) or "[]")
        except (TypeError, json.JSONDecodeError):
            s[field] = []
    return s


def get_latest_report() -> dict | None:
    """最新一版报告 + 其建议列表（按优先级降序）。"""
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM reports ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        return None
    report = dict(row)
    try:
        report["params"] = json.loads(report.get("params") or "{}")
    except json.JSONDecodeError:
        report["params"] = {}
    try:
        report["suggestion_ids"] = json.loads(report.get("suggestion_ids") or "[]")
    except json.JSONDecodeError:
        report["suggestion_ids"] = []
    sug_rows = conn.execute(
        "SELECT * FROM suggestions WHERE report_id = ? ORDER BY priority_score DESC",
        (report["id"],),
    ).fetchall()
    conn.close()
    report["suggestions"] = [_suggestion_row_to_dict(r) for r in sug_rows]
    return report


def get_report_history(limit: int = 20) -> list[dict]:
    """报告版本历史（不含正文）。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, title, params, model, created_at, suggestion_ids FROM reports "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["params"] = json.loads(d.get("params") or "{}")
        except json.JSONDecodeError:
            d["params"] = {}
        try:
            d["suggestion_count"] = len(json.loads(d.get("suggestion_ids") or "[]"))
        except json.JSONDecodeError:
            d["suggestion_count"] = 0
        d.pop("suggestion_ids", None)
        out.append(d)
    return out


def get_suggestion_evidence_comment_ids(suggestion_id: str) -> list[str]:
    """取一条建议的证据评论 id 列表。"""
    conn = get_db()
    row = conn.execute(
        "SELECT evidence_comment_ids FROM suggestions WHERE id = ?", (suggestion_id,)
    ).fetchone()
    conn.close()
    if not row or not row["evidence_comment_ids"]:
        return []
    try:
        ids = json.loads(row["evidence_comment_ids"])
        return ids if isinstance(ids, list) else []
    except json.JSONDecodeError:
        return []


def get_comments_by_ids(comment_ids: list[str]) -> list[dict]:
    """按 id 列表取评论详情（含分析与品牌），顺序保持输入顺序。
    译文优先级：comment_translations 缓存 > analyses.translation_zh > summary_zh 兜底；
    translation_source 标记来源（cache/analysis/summary），前端据此区分「翻译」与「摘要」。"""
    if not comment_ids:
        return []
    placeholders = ",".join("?" for _ in comment_ids)
    conn = get_db()
    rows = conn.execute(f"""
        SELECT c.id, c.platform, c.content_clean, c.source_url, c.like_count, c.language,
               b.name AS brand_name,
               a.severity, a.sentiment_score, a.summary_zh, a.translation_zh, a.pain_tags,
               a.context_environment, a.user_action, a.pain_root_cause,
               t.translation_zh AS cached_translation
        FROM comments c
        LEFT JOIN brands b ON c.brand_id = b.id
        LEFT JOIN analyses a ON a.comment_id = c.id
        LEFT JOIN comment_translations t ON t.comment_id = c.id
        WHERE c.id IN ({placeholders})
    """, comment_ids).fetchall()
    conn.close()
    by_id = {r["id"]: dict(r) for r in rows}
    result = []
    for cid in comment_ids:
        if cid not in by_id:
            continue
        row = by_id[cid]
        cached = row.pop("cached_translation")
        row["translation_zh"], row["translation_source"] = _pick_translation(
            cached, row.get("translation_zh"), row.get("summary_zh")
        )
        result.append(row)
    return result


def get_comment_translations(comment_ids: list[str]) -> dict[str, str]:
    """批量读取翻译缓存，返回 {comment_id: translation_zh}。"""
    if not comment_ids:
        return {}
    placeholders = ",".join("?" for _ in comment_ids)
    conn = get_db()
    rows = conn.execute(f"""
        SELECT comment_id, translation_zh FROM comment_translations
        WHERE comment_id IN ({placeholders})
    """, comment_ids).fetchall()
    conn.close()
    return {r["comment_id"]: r["translation_zh"] for r in rows}


def save_comment_translations(items: list[tuple[str, str, str]]) -> int:
    """批量保存翻译缓存，items 为 (comment_id, translation_zh, llm_model)。"""
    if not items:
        return 0
    conn = get_db()
    cur = conn.executemany("""
        INSERT INTO comment_translations (comment_id, translation_zh, llm_model, created_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(comment_id) DO UPDATE SET
            translation_zh = excluded.translation_zh,
            llm_model = excluded.llm_model,
            created_at = datetime('now')
    """, items)
    conn.commit()
    conn.close()
    return cur.rowcount


if __name__ == "__main__":
    init_db()
    init_default_brands()
    print(f"数据库初始化完成: {settings.DB_PATH}")
