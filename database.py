"""
数据库层 — SQLite 存储
MVP 阶段用 SQLite，后续可平滑迁移到 PostgreSQL
"""
import sqlite3
import uuid
import json
from datetime import datetime
from pathlib import Path
from config import settings


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
            title TEXT,
            channel TEXT,
            view_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            published_at TEXT,
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
            summary_zh TEXT NOT NULL,
            llm_model TEXT NOT NULL,
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

    # 索引
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_brand ON comments(brand_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_video ON comments(video_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comments_analyzed ON comments(analyzed)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_analyses_severity ON analyses(severity)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_analyses_sentiment ON analyses(sentiment_score)")

    conn.commit()
    conn.close()


def insert_brand(name: str, search_keyword: str) -> str:
    """插入品牌，返回 brand_id"""
    conn = get_db()
    brand_id = str(uuid.uuid4())
    try:
        conn.execute(
            "INSERT INTO brands (id, name, search_keyword) VALUES (?, ?, ?)",
            (brand_id, name, search_keyword)
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
    """
    conn = get_db()
    comment_id = str(uuid.uuid4())
    try:
        conn.execute("""
            INSERT INTO comments
                (id, platform, original_id, video_id, brand_id, content, content_clean,
                 language, author, like_count, posted_at, sentiment_pre, analyzed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
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
            data.get("sentiment_pre"),
        ))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False


def insert_analysis(comment_id: str, result: dict, model: str) -> str:
    """插入分析结果"""
    conn = get_db()
    analysis_id = str(uuid.uuid4())
    conn.execute("""
        INSERT OR REPLACE INTO analyses
            (id, comment_id, sentiment_score, pain_categories, pain_tags,
             severity, user_solution, product_match, summary_zh, llm_model)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        analysis_id,
        comment_id,
        result["sentiment_score"],
        json.dumps(result.get("pain_categories", []), ensure_ascii=False),
        json.dumps(result.get("pain_tags", []), ensure_ascii=False),
        result["severity"],
        result.get("user_solution"),
        result.get("product_match"),
        result["summary_zh"],
        model,
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
        # 指定品牌 — 直接取该品牌最新评论
        rows = conn.execute("""
            SELECT c.*, b.name as brand_name
            FROM comments c
            LEFT JOIN brands b ON c.brand_id = b.id
            WHERE c.analyzed = 0 AND b.name = ?
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
        WHERE c.analyzed = 0
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
               c.platform, c.like_count, c.original_id,
               b.name as brand_name,
               v.video_id as yt_video_id, v.title as video_title,
               a.sentiment_score, a.pain_categories, a.pain_tags,
               a.severity, a.user_solution, a.product_match, a.summary_zh,
               a.llm_model, a.analyzed_at
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




def get_setting(key: str, default: str = "") -> str:
    """读取单个配置项"""
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


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
    return {row["key"]: row["value"] for row in rows}


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
            set_setting(f"{provider}_api_key", key)
    if "models" in config:
        for provider, model in config["models"].items():
            set_setting(f"{provider}_model", model)


# ==================== 品牌管理 ====================

def add_brand(name: str, search_keyword: str) -> dict:
    """添加品牌，返回 {id, name, search_keyword}"""
    conn = get_db()
    brand_id = str(uuid.uuid4())
    try:
        conn.execute(
            "INSERT INTO brands (id, name, search_keyword) VALUES (?, ?, ?)",
            (brand_id, name, search_keyword)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise ValueError(f"品牌 '{name}' 已存在")
    conn.close()
    return {"id": brand_id, "name": name, "search_keyword": search_keyword}


def update_brand(brand_id: str, name: str, search_keyword: str) -> bool:
    """修改品牌信息"""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE brands SET name = ?, search_keyword = ? WHERE id = ?",
            (name, search_keyword, brand_id)
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
        SELECT b.id, b.name, b.search_keyword, b.created_at,
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
        ("Blackview", "Blackview rugged phone review"),
        ("Ulefone", "Ulefone Armor review"),
        ("Doogee", "Doogee rugged phone review"),
        ("Oukitel", "Oukitel rugged phone review"),
        ("Unihertz", "Unihertz rugged phone review"),
    ]
    for name, keyword in competitors:
        insert_brand(name, keyword)


if __name__ == "__main__":
    init_db()
    init_default_brands()
    print(f"数据库初始化完成: {settings.DB_PATH}")
