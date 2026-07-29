"""
FastAPI 主应用 — VoC 痛点挖掘平台 API
支持多 LLM 提供商配置 + 品牌管理
"""
import logging
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import sys
from pathlib import Path

from config import settings
from database import (
    init_db, init_default_brands, get_pain_points, get_stats, get_db,
    get_llm_config, save_llm_config, get_all_brands,
    add_brand, update_brand, delete_brand,
    get_tag_distribution, get_brand_tag_matrix, get_model_pain_ranking,
    get_priority_matrix, get_severity_distribution, get_sentiment_distribution,
    get_insights_summary,
)
from crawler import crawl_competitor, list_platforms
from analyzer import analyze_batch
from llm_provider import LLMClient, list_providers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="VoC 痛点挖掘平台", version="0.2.0")

if getattr(sys, '_MEIPASS', None):
    STATIC_DIR = Path(sys._MEIPASS) / "static"
else:
    STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# === Pydantic 请求模型 ===

class LLMConfigModel(BaseModel):
    provider: str = ""
    api_keys: dict = {}
    models: dict = {}


class BrandModel(BaseModel):
    name: str
    search_keyword: str


class TestConnectionModel(BaseModel):
    provider: str
    api_key: str
    model: str = ""


# === 生命周期 ===

@app.on_event("startup")
def startup():
    init_db()
    init_default_brands()
    logger.info(f"数据库就绪: {settings.DB_PATH}")


# === 页面 ===

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# === 数据查询 ===

@app.get("/api/stats")
async def api_stats():
    return get_stats()


@app.get("/api/pain-points")
async def api_pain_points(
    brand: str | None = None,
    platform: str | None = None,
    min_severity: int = 1,
    limit: int = 100,
):
    return get_pain_points(brand=brand, platform=platform, min_severity=min_severity, limit=limit)


@app.get("/api/comments")
async def api_comments(
    analyzed: int | None = None,
    brand: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    """获取评论列表（支持分页）"""
    conn = get_db()
    query = """
        SELECT c.id, c.content, c.content_clean, c.language, c.author,
               c.platform, c.like_count, c.posted_at, c.analyzed, c.crawled_at,
               b.name as brand_name, v.title as video_title
        FROM comments c
        LEFT JOIN brands b ON c.brand_id = b.id
        LEFT JOIN videos v ON c.video_id = v.id
        WHERE 1=1
    """
    params: list = []
    if analyzed is not None:
        query += " AND c.analyzed = ?"
        params.append(analyzed)
    if brand:
        query += " AND b.name = ?"
        params.append(brand)
    query += " ORDER BY c.crawled_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/comments/grouped")
async def api_comments_grouped():
    """获取评论按品牌分组的统计 + 每组前 5 条评论"""
    conn = get_db()
    # 品牌维度统计
    brand_stats = conn.execute("""
        SELECT b.id, b.name,
               COUNT(c.id) as comment_count,
               SUM(CASE WHEN c.analyzed = 1 THEN 1 ELSE 0 END) as analyzed_count
        FROM brands b
        LEFT JOIN comments c ON c.brand_id = b.id
        GROUP BY b.id, b.name
        ORDER BY b.name
    """).fetchall()

    result = []
    for row in brand_stats:
        brand_name = row["name"]
        # 获取该品牌前 5 条评论
        sample_comments = conn.execute("""
            SELECT c.id, c.content_clean, c.content, c.language, c.author,
                   c.like_count, c.analyzed, c.posted_at, c.platform
            FROM comments c
            WHERE c.brand_id = ?
            ORDER BY c.like_count DESC, c.crawled_at DESC
            LIMIT 5
        """, (row["id"],)).fetchall()
        result.append({
            "brand_id": row["id"],
            "brand_name": brand_name,
            "comment_count": row["comment_count"],
            "analyzed_count": row["analyzed_count"],
            "comments": [dict(c) for c in sample_comments],
        })

    conn.close()
    return result


@app.get("/api/analyses")
async def api_analyses(
    brand: str | None = None,
    min_severity: int = 1,
    limit: int = 200,
    offset: int = 0,
):
    """获取所有分析结果（含评论原文 + 分析详情）"""
    conn = get_db()
    query = """
        SELECT c.id, c.content, c.content_clean, c.language, c.author,
               c.platform, c.like_count, c.posted_at, c.original_id,
               b.name as brand_name, v.title as video_title,
               v.video_id as yt_video_id,
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
    query += " ORDER BY a.severity DESC, a.sentiment_score ASC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# === LLM 配置 ===

@app.get("/api/llm/providers")
async def api_providers():
    return list_providers()


@app.get("/api/llm/config")
async def api_get_config():
    config = get_llm_config()
    # API Key 脱敏
    masked = {}
    for k, v in config["api_keys"].items():
        if v and len(v) > 8:
            masked[k] = v[:4] + "***" + v[-4:]
        else:
            masked[k] = v
    return {"provider": config["provider"], "api_keys": masked, "models": config["models"]}


@app.post("/api/llm/config")
async def api_save_config(config: LLMConfigModel):
    save_llm_config(config.model_dump())
    return {"status": "ok"}


@app.post("/api/llm/test")
async def api_test_connection(req: TestConnectionModel):
    try:
        client = LLMClient(provider=req.provider, api_key=req.api_key, model=req.model)
        ok, msg = client.test_connection()
        return {"success": ok, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}


# === 品牌管理 ===

@app.get("/api/brands")
async def api_brands():
    return get_all_brands()


@app.post("/api/brands")
async def api_add_brand(brand: BrandModel):
    try:
        return add_brand(brand.name, brand.search_keyword)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/brands/{brand_id}")
async def api_update_brand(brand_id: str, brand: BrandModel):
    try:
        update_brand(brand_id, brand.name, brand.search_keyword)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/brands/{brand_id}")
async def api_delete_brand(brand_id: str):
    delete_brand(brand_id)
    return {"status": "ok"}


# === 抓取 & 分析 ===

@app.post("/api/crawl")
async def api_crawl(
    brand_name: str = "",
    search_keyword: str = "",
    max_videos: int = 5,
    platform: str = "youtube",
):
    """在后台线程执行抓取，避免阻塞事件循环。platform: youtube/reddit/instagram/tiktok"""
    from fastapi.concurrency import run_in_threadpool

    def _do_crawl():
        if brand_name and search_keyword:
            result = crawl_competitor(brand_name, search_keyword, max_videos=max_videos, platform=platform)
            return {"results": [result]}
        else:
            brands = get_all_brands()
            results = []
            for b in brands:
                r = crawl_competitor(b["name"], b["search_keyword"], max_videos=max_videos, platform=platform)
                results.append(r)
            return {"results": results}

    return await run_in_threadpool(_do_crawl)


@app.get("/api/platforms")
async def api_platforms():
    """返回支持的抓取平台列表"""
    return list_platforms()


@app.post("/api/analyze")
async def api_analyze(limit: int = 500, brand: str | None = None):
    """在后台线程执行分析，避免阻塞事件循环。brand 指定时只分析该品牌。"""
    from fastapi.concurrency import run_in_threadpool
    return await run_in_threadpool(analyze_batch, limit=limit, brand=brand)


# === 第三层：结构化洞察 ===

@app.get("/api/insights/tag-distribution")
async def api_tag_distribution():
    """痛点标签分布"""
    return get_tag_distribution()


@app.get("/api/insights/brand-tag-matrix")
async def api_brand_tag_matrix():
    """品牌×痛点交叉矩阵"""
    return get_brand_tag_matrix()


@app.get("/api/insights/model-ranking")
async def api_model_ranking():
    """型号痛点排名"""
    return get_model_pain_ranking()


@app.get("/api/insights/priority-matrix")
async def api_priority_matrix():
    """优先级矩阵"""
    return get_priority_matrix()


@app.get("/api/insights/severity-distribution")
async def api_severity_distribution():
    """严重度分布"""
    return get_severity_distribution()


@app.get("/api/insights/sentiment-distribution")
async def api_sentiment_distribution():
    """情感分布"""
    return get_sentiment_distribution()


# === 第四层：AI 改良建议 ===

@app.get("/api/insights/summary")
async def api_insights_summary():
    """汇总痛点数据（前端用此数据调LLM生成报告）"""
    return get_insights_summary()


@app.post("/api/insights/generate-report")
async def api_generate_report():
    """AI 生成改良建议报告（后端调LLM）"""
    from fastapi.concurrency import run_in_threadpool

    def _generate():
        summary = get_insights_summary()
        if not summary["top_pains"]:
            return {"error": "暂无足够的分析数据，请先运行 AI 分析后再生成报告"}

        config = get_llm_config()
        provider = config["provider"]
        if not provider:
            return {"error": "未配置 LLM 提供商，请先在设置页面配置"}

        from llm_provider import LLMClient
        from database import get_setting

        api_key = config["api_keys"].get(provider, "")
        model = config["models"].get(provider, "")
        client = LLMClient(provider=provider, api_key=api_key, model=model)

        # 构建提示词
        import json as _json
        top_pains_text = "\n".join([
            f"- {p['tag']}: 共{p['count']}条提及 (严重度3:{p['sev3']}, 严重度2:{p['sev2']})"
            for p in summary["top_pains"]
        ])
        brand_text = "\n".join([
            f"- {b['brand']}: 总{b['total']}条痛点, 高严重度{b['sev3']}条, 平均严重度{b['avg_sev']}"
            for b in summary["brand_comparison"]
        ])
        solutions_text = "\n".join([
            f"- [{s['brand']}] 痛点: {_json.loads(s['pain_tags']) if s['pain_tags'] else []} → 用户建议: {s['user_solution']}"
            for s in summary["user_solutions"][:15]
        ])
        high_sev_text = "\n".join([
            f"- [{s['brand']}] {s['summary_zh']}"
            for s in summary["high_severity_samples"][:10]
        ])

        prompt = f"""你是一名三防手机产品总监，请基于以下竞品评论痛点数据，生成产品改良建议报告。

## 痛点频率排行
{top_pains_text}

## 各品牌痛点对比
{brand_text}

## 高严重度痛点摘要
{high_sev_text}

## 用户提出的改良方案
{solutions_text}

## 输出要求（Markdown格式）
请生成以下内容：

### 一、高频痛点改良建议
针对 Top 5 高频痛点，分别给出：
- 痛点描述
- 影响范围（哪些品牌/型号）
- 改良建议（具体技术方向）
- 优先级（P0/P1/P2）

### 二、竞品差距分析
找出各品牌在痛点维度的差异，输出：
- 某品牌在某方面明显较差（需改进）
- 某品牌在某方面表现较好（可借鉴）
- 具体差距数据

### 三、微创新机会
从用户方案中提炼可执行的微创新点：
- 创新方向
- 用户原话依据
- 实现难度（高/中/低）

### 四、改良优先级清单
综合频率×严重度×用户需求，输出 Top 10 改良优先级清单。
"""

        try:
            report = client.generate(prompt, temperature=0.3, max_tokens=4000)
            return {"report": report, "generated_at": _get_now()}
        except Exception as e:
            return {"error": f"生成失败: {type(e).__name__}: {e}"}

    return await run_in_threadpool(_generate)


def _get_now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
