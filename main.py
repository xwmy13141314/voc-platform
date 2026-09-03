"""
FastAPI 主应用 — VoC 痛点挖掘平台 API
支持多 LLM 提供商配置 + 品牌管理
"""
import logging
import json
import os
import threading
from datetime import datetime
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
    get_emotion_distribution, get_positive_tags_distribution, get_user_solutions,
    get_insights_summary,
    get_sev3_by_brand, get_solution_tags, get_analysis_progress, get_field_fill_rates,
    reset_analyses_for_reanalysis,
    create_job, update_job, get_job, get_recent_jobs, get_active_job,
    request_job_cancel, is_job_cancel_requested,
    get_comment_analysis, update_analysis,
    get_reddit_config, save_reddit_config,
    get_reddit_cookie_config, save_reddit_cookie_config,
    get_instagram_config, save_instagram_config,
    get_facebook_config, save_facebook_config,
    add_spec, get_specs, update_spec, delete_spec, get_spec_regression,
    get_analysis_with_tuple, get_top_pain_comments,
    add_gold_standard, get_gold_standards, delete_gold_standard, get_gold_standard_report,
)
from crawler import crawl_competitor, crawl_all_brands_reddit, list_platforms
from analyzer import analyze_batch
from llm_provider import LLMClient, list_providers
from sources.quality_filter import load_filter_config, save_filter_config, recompute_all_filters
from version import APP_NAME, APP_VERSION, SCHEMA_VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title=APP_NAME, version=APP_VERSION)
INSTANCE_TOKEN = os.environ.get("VOC_INSTANCE_TOKEN", "source-run")

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
    brand_type: str = "competitor"


class BrandUpdateModel(BaseModel):
    name: str
    search_keyword: str
    brand_type: str = "competitor"


class TestConnectionModel(BaseModel):
    provider: str
    api_key: str
    model: str = ""


class AnalysisUpdateModel(BaseModel):
    sentiment_score: int
    pain_categories: list[str] = []
    pain_tags: list[str] = []
    severity: int
    user_solution: str | None = None
    product_match: str | None = None
    translation_zh: str = ""


class SpecModel(BaseModel):
    brand_id: str
    model: str = ""
    spec_category: str
    spec_key: str
    spec_value: str
    spec_unit: str = ""
    source_url: str = ""


class SpecUpdateModel(BaseModel):
    brand_id: str | None = None
    model: str = ""
    spec_category: str
    spec_key: str
    spec_value: str
    spec_unit: str = ""
    source_url: str = ""


class ProposalRequestModel(BaseModel):
    pain_tag: str
    brand: str | None = None
    min_severity: int = 2
    bom_constraint: str = ""
    mold_constraint: str = ""


class GoldStandardModel(BaseModel):
    comment_id: str
    expected_tags: str = ""
    expected_severity: int | None = None
    expected_sentiment: int | None = None
    expected_four_tuple: str = ""


class RedditConfigModel(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    username: str = ""
    password: str = ""


class RedditCookieConfigModel(BaseModel):
    method: str = "browser"  # browser / manual
    browser: str = "chrome"  # chrome / firefox / edge / opera / brave
    cookies: str = ""


class InstagramConfigModel(BaseModel):
    username: str = ""
    password: str = ""


class FacebookConfigModel(BaseModel):
    cookies: str = ""


_JOB_THREADS: dict[str, threading.Thread] = {}
_JOB_CREATE_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def escapeHtmlJs(text: str) -> str:
    """Escape HTML special chars for safe inline insertion."""
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


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
               c.platform, c.like_count, c.posted_at, c.source_url, c.analyzed, c.crawled_at,
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
                   c.like_count, c.analyzed, c.posted_at, c.platform, c.source_url
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
               c.platform, c.like_count, c.posted_at, c.original_id, c.source_url,
               b.name as brand_name, v.title as video_title,
               v.external_id as external_video_id, v.platform as video_platform,
               v.source_url as video_source_url,
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
        if v:
            masked[k] = (v[:2] + "***" + v[-2:]) if len(v) > 4 else "***"
        else:
            masked[k] = ""
    return {"provider": config["provider"], "api_keys": masked, "models": config["models"]}


@app.get("/api/health")
async def api_health():
    db_ok = False
    try:
        conn = get_db()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_ok = True
    except Exception:
        logger.exception("health check database failure")
    jiter_ok = False
    try:
        from jiter import from_json
        jiter_ok = from_json(b'{"ok":true}').get("ok") is True
    except Exception:
        logger.exception("health check jiter failure")
    return {
        "status": "ok" if db_ok and jiter_ok else "degraded",
        "version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "instance_token": INSTANCE_TOKEN,
        "database": db_ok,
        "database_path": str(settings.DB_PATH.resolve()),
        "jiter": jiter_ok,
    }


@app.post("/api/llm/config")
async def api_save_config(config: LLMConfigModel):
    payload = config.model_dump()
    # 前端保存脱敏配置时不应覆盖本地原 Key。
    existing = get_llm_config()
    for provider, key in list(payload.get("api_keys", {}).items()):
        if not key or "***" in key:
            payload["api_keys"][provider] = existing["api_keys"].get(provider, "")
    save_llm_config(payload)
    return {"status": "ok"}


@app.post("/api/llm/test")
async def api_test_connection(req: TestConnectionModel):
    try:
        client = LLMClient(provider=req.provider, api_key=req.api_key, model=req.model)
        ok, msg = client.test_connection()
        return {"success": ok, "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)}


# === Reddit OAuth ?? ===

@app.get("/api/reddit/config")
async def api_get_reddit_config():
    return get_reddit_config()


@app.post("/api/reddit/config")
async def api_save_reddit_config(config: RedditConfigModel):
    payload = config.model_dump()
    save_reddit_config(payload)
    return {"status": "ok"}


@app.post("/api/reddit/test")
async def api_test_reddit():
    """Test Reddit OAuth connection by fetching a small search."""
    try:
        import requests as _requests
        from database import get_all_settings
        s = get_all_settings()
        client_id = s.get("reddit_client_id", "")
        client_secret = s.get("reddit_client_secret", "")
        username = s.get("reddit_username", "")
        password = s.get("reddit_password", "")
        if not client_id or not client_secret:
            return {"success": False, "message": "???? client_id ? client_secret"}
        auth = _requests.auth.HTTPBasicAuth(client_id, client_secret)
        ua = crawler._build_reddit_ua(username)
        if username and password:
            data = {"grant_type": "password", "username": username, "password": password}
        else:
            data = {"grant_type": "client_credentials"}
        resp = _requests.post("https://www.reddit.com/api/v1/access_token",
                              auth=auth, data=data, headers={"User-Agent": ua}, timeout=15)
        if resp.status_code != 200:
            return {"success": False, "message": f"OAuth ?? (HTTP {resp.status_code}): {resp.text[:200]}"}
        token = resp.json().get("access_token", "")
        if not token:
            return {"success": False, "message": "???? access_token??????"}
        # Quick search test
        resp2 = _requests.get("https://oauth.reddit.com/search",
                              params={"q": "test", "limit": 1},
                              headers={"Authorization": f"Bearer {token}", "User-Agent": ua},
                              timeout=15)
        if resp2.status_code == 200:
            return {"success": True, "message": "Reddit OAuth ????????????"}
        return {"success": False, "message": f"Token ????????? (HTTP {resp2.status_code})"}
    except Exception as e:
        return {"success": False, "message": str(e)}


# === Reddit Cookie 配置（方案B）===

@app.get("/api/reddit/cookie/config")
async def api_get_reddit_cookie_config():
    return get_reddit_cookie_config()


@app.post("/api/reddit/cookie/config")
async def api_save_reddit_cookie_config(config: RedditCookieConfigModel):
    payload = config.model_dump()
    save_reddit_cookie_config(payload)
    # 重置 Cookie 会话，使下次抓取时重新初始化
    import crawler
    crawler._reddit_reset_cookie_session()
    return {"status": "ok"}


@app.post("/api/reddit/cookie/test")
async def api_test_reddit_cookie():
    """测试 Reddit Cookie 认证连接。"""
    try:
        import crawler
        # 重置 session 以使用最新配置
        crawler._reddit_reset_cookie_session()
        # 读取当前配置用于诊断
        cfg = crawler._reddit_get_cookie_config()
        # 尝试搜索测试
        resp = crawler._reddit_cookie_get(
            "/search.json",
            params={"q": "test", "limit": 1, "raw_json": 1},
            log_tag="cookie-test",
        )
        if resp is not None:
            try:
                data = resp.json()
                children = (data.get("data") or {}).get("children") or []
                return {
                    "success": True,
                    "message": f"Reddit Cookie 认证成功！测试搜索返回 {len(children)} 条结果",
                }
            except Exception:
                return {"success": True, "message": "Reddit Cookie 认证成功（响应解析异常但 HTTP 200）"}
        else:
            # 诊断具体失败原因
            session = crawler._reddit_get_cookie_session()
            if session is None:
                method = cfg.get("method", "browser")
                has_cookies = bool(cfg.get("cookies", ""))
                if method == "manual" and not has_cookies:
                    return {
                        "success": False,
                        "message": "手动模式下未检测到 cookies。请在文本框中粘贴 cookies 后点击「测试连接」（会自动保存）。",
                    }
                if method == "browser":
                    return {
                        "success": False,
                        "message": "浏览器模式下未能提取 cookies。请关闭 Chrome/Edge 后点「提取 Cookies」，或切换为手动模式粘贴 cookies。",
                    }
                return {
                    "success": False,
                    "message": "cookies 已保存但会话创建失败。可能是网络问题或 cookies 已过期，请刷新浏览器 cookies 后重新粘贴。",
                }
            return {
                "success": False,
                "message": "Cookie 认证请求失败（cookies 可能已过期或被 Reddit 拒绝）。请重新获取 cookies 粘贴。",
            }
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/reddit/cookie/extract")
async def api_extract_reddit_cookies():
    """提取浏览器 cookies 并缓存。需要关闭 Chrome/Edge 浏览器。"""
    try:
        import crawler
        from database import get_setting
        browser = get_setting("reddit_browser", "chrome")
        result = crawler.reddit_extract_and_cache_cookies(browser)
        return result
    except Exception as e:
        return {"success": False, "message": str(e), "cookie_count": 0}


@app.post("/api/reddit/diagnose")
async def api_reddit_diagnose():
    """Reddit 全面诊断：测试网络连通性、OAuth、PRAW、Cookie 等各路径。"""
    import crawler
    from database import get_all_settings
    results = {"checks": [], "recommendation": ""}

    s = get_all_settings()
    client_id = s.get("reddit_client_id", "")
    client_secret = s.get("reddit_client_secret", "")

    # 1. 检查 OAuth 凭据是否配置
    has_oauth = bool(client_id and client_secret)
    results["checks"].append({
        "name": "OAuth 凭据",
        "status": "pass" if has_oauth else "fail",
        "detail": "Client ID 和 Client Secret 已配置" if has_oauth
                  else "未配置 OAuth 凭据。请到 https://www.reddit.com/prefs/apps 创建 script 类型应用",
    })

    if not has_oauth:
        results["recommendation"] = (
            "请配置 Reddit OAuth 凭据：\n"
            "1. 浏览器打开 https://www.reddit.com/prefs/apps\n"
            "2. 页面底部 'create another app...' → 选择 'script' 类型\n"
            "3. 名称填 voc-platform，redirect URI 填 http://localhost\n"
            "4. 创建后复制 Client ID（应用名下方短字符串）和 Client Secret\n"
            "5. 在设置页面填入并保存"
        )
        return results

    # 2. 重置 PRAW 实例并测试创建
    crawler._reddit_reset_praw()
    praw_instance = crawler._get_praw_instance()
    praw_ok = praw_instance is not None
    results["checks"].append({
        "name": "PRAW 实例创建",
        "status": "pass" if praw_ok else "fail",
        "detail": "PRAW 实例创建成功" if praw_ok else "PRAW 实例创建失败，请检查凭据是否正确",
    })

    if not praw_ok:
        results["recommendation"] = "PRAW 实例创建失败，请检查 Client ID 和 Client Secret 是否正确"
        return results

    # 3. 测试 PRAW 搜索
    try:
        test_posts = crawler._praw_search_posts("test", limit=1)
        results["checks"].append({
            "name": "PRAW 搜索测试",
            "status": "pass",
            "detail": f"搜索成功，返回 {len(test_posts)} 条结果",
        })
        results["recommendation"] = "PRAW 官方 API 工作正常！可以正常抓取 Reddit 数据。"
    except Exception as exc:
        err_msg = str(exc)
        results["checks"].append({
            "name": "PRAW 搜索测试",
            "status": "fail",
            "detail": f"搜索失败: {err_msg[:200]}",
        })
        if "401" in err_msg or "OAuth" in err_msg:
            results["recommendation"] = "OAuth 认证失败，请检查 Client ID 和 Client Secret 是否正确"
        elif "429" in err_msg:
            results["recommendation"] = "被限流，请稍后再试"
        elif "Connection" in err_msg or "timeout" in err_msg.lower() or "SSLError" in err_msg:
            results["recommendation"] = (
                "网络连接失败，可能原因：\n"
                "1. 当前网络无法访问 Reddit（如需使用代理/VPN）\n"
                "2. 防火墙阻止了对 oauth.reddit.com 的访问\n"
                "3. DNS 解析失败"
            )
        else:
            results["recommendation"] = f"PRAW 搜索失败: {err_msg[:300]}"

    return results


# === Instagram 账号配置 ===

@app.get("/api/instagram/config")
async def api_get_instagram_config():
    return get_instagram_config()


@app.post("/api/instagram/config")
async def api_save_instagram_config(config: InstagramConfigModel):
    payload = config.model_dump()
    save_instagram_config(payload)
    # 重置 Instaloader 实例，使下次抓取时重新登录
    import crawler
    crawler._instaloader_instance = None
    crawler._instaloader_username = None
    return {"status": "ok"}


# === Facebook Cookies 配置 ===

@app.get("/api/facebook/config")
async def api_get_facebook_config():
    return get_facebook_config()


@app.post("/api/facebook/config")
async def api_save_facebook_config(config: FacebookConfigModel):
    payload = config.model_dump()
    save_facebook_config(payload)
    return {"status": "ok"}


# === 导出配置 ===

@app.get("/api/export/config")
async def api_get_export_config():
    from database import get_setting
    export_dir = get_setting("export_dir", "")
    if not export_dir:
        # 默认桌面路径
        export_dir = str(Path.home() / "Desktop")
    return {"export_dir": export_dir}


class ExportDirModel(BaseModel):
    export_dir: str = ""


@app.post("/api/export/config")
async def api_save_export_config(config: ExportDirModel):
    from database import set_setting
    set_setting("export_dir", config.export_dir)
    return {"status": "ok"}


@app.get("/api/export/pick-folder")
async def api_pick_export_folder():
    """弹出系统文件夹选择对话框，返回所选路径"""
    import threading
    result = {"path": ""}

    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            folder = filedialog.askdirectory(title="选择导出目录", parent=root)
            root.destroy()
            if folder:
                result["path"] = folder
        except Exception as exc:
            logger.exception("文件夹选择失败: %s", exc)

    # tkinter 必须在主线程或独立线程运行
    t = threading.Thread(target=_pick, daemon=True)
    t.start()
    t.join(timeout=120)  # 最多等 2 分钟
    if result["path"]:
        return {"path": result["path"]}
    return {"path": ""}


class ExportHtmlModel(BaseModel):
    html: str
    filename: str = "VoC报告.html"
    charts: dict | None = None
    theme: str = "light"


@app.post("/api/export/html")
async def api_export_html(req: ExportHtmlModel):
    """将报告 HTML 写入用户配置的导出目录，返回完整路径"""
    from database import get_setting
    import os

    export_dir = get_setting("export_dir", "")
    if not export_dir:
        export_dir = str(Path.home() / "Desktop")

    # 确保目录存在
    Path(export_dir).mkdir(parents=True, exist_ok=True)

    # 安全文件名
    safe_name = req.filename.replace("/", "_").replace("\\", "_").strip()
    if not safe_name.endswith(".html"):
        safe_name += ".html"

    # 如果有图表数据，构建包含 ECharts 的完整独立 HTML
    final_html = req.html
    if req.charts:
        echarts_path = STATIC_DIR / "echarts.min.js"
        echarts_js = ""
        if echarts_path.exists():
            echarts_js = echarts_path.read_text(encoding="utf-8")

        is_dark = req.theme == "dark"
        bg = "#0f1419" if is_dark else "#ffffff"
        ink = "#e6e6e6" if is_dark else "#1a2332"
        accent = "#00d4ff" if is_dark else "#0284c7"
        muted = "#8a9bb4" if is_dark else "#64748b"
        border = "#2a3a52" if is_dark else "#e2e8f0"
        bg2 = "#1a2332" if is_dark else "#f1f5f9"

        import json as _json2
        charts_json = _json2.dumps(req.charts, ensure_ascii=False)

        final_html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VoC 痛点挖掘平台 - AI 改良建议报告</title>
<style>
  body{{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;background:{bg};color:{ink};line-height:1.8;max-width:960px;margin:0 auto;padding:2rem;}}
  h1{{color:{accent};font-size:1.5rem;border-bottom:2px solid {accent};padding-bottom:0.5rem;}}
  h2{{color:{accent};font-size:1.2rem;margin-top:2rem;}}
  h3{{color:{accent};font-size:1.05rem;margin-top:1.5rem;}}
  table{{width:100%;border-collapse:collapse;margin:1rem 0;}}
  th,td{{border:1px solid {border};padding:0.5rem 0.8rem;text-align:left;font-size:0.85rem;}}
  th{{background:{bg2};color:{muted};}}
  tr:nth-child(even){{background:{"rgba(255,255,255,0.02)" if is_dark else "#f8fafc"};}}
  code{{background:{bg2};padding:2px 6px;border-radius:3px;font-family:'Consolas',monospace;font-size:0.85rem;}}
  pre{{background:{bg2};padding:1rem;border-radius:6px;overflow-x:auto;}}
  blockquote{{border-left:3px solid {accent};padding-left:1rem;color:{muted};margin:1rem 0;}}
  a{{color:{accent};}}
  .stats-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:1rem;margin-bottom:2rem;}}
  .stat-card{{text-align:center;padding:1rem;border-radius:8px;background:{bg2};}}
  .stat-num{{font-size:1.8rem;font-weight:700;color:{accent};}}
  .stat-label{{font-size:0.75rem;color:{muted};}}
  .chart-section{{margin-bottom:1.5rem;}}
  .report-meta{{text-align:right;color:{muted};font-size:0.8rem;margin-top:2rem;border-top:1px solid {border};padding-top:0.5rem;}}
  @media print{{body{{background:#fff;color:#000;max-width:none;}} h1,h2,h3{{color:#000 !important;}} th{{background:#f0f0f0 !important;}} .stat-num{{color:#000 !important;}}}}
</style>
</head>
<body>
<h1>VoC 痛点挖掘平台 — AI 改良建议报告</h1>
<div id="reportContent"></div>
<div class="report-meta">生成时间: {escapeHtmlJs(req.charts.get("generated_at",""))} | VoC 痛点挖掘平台 v{APP_VERSION}</div>
<script>
{echarts_js}
</script>
<script>
(function(){{
  var charts={charts_json};
  var isDark={str(is_dark).lower()};
  var bgColor=isDark?'#0f1419':'#ffffff';
  var accentColor=isDark?'#00d4ff':'#0284c7';
  var dangerColor='#f87171';
  var mutedColor=isDark?'#8a9bb4':'#64748b';
  var textColor=mutedColor;
  var gridColor=isDark?'#2a3a52':'#e2e8f0';
  var reportHtml={_json2.dumps(req.html, ensure_ascii=False)};
  var html='<div class="report-content">';
  // 统计卡片
  var s=charts.stats||{{}};
  html+='<div class="stats-grid">';
  html+='<div class="stat-card"><div class="stat-num">'+(s.total_comments||0)+'</div><div class="stat-label">总评论数</div></div>';
  html+='<div class="stat-card"><div class="stat-num">'+(s.total_analyses||0)+'</div><div class="stat-label">已分析数</div></div>';
  html+='<div class="stat-card"><div class="stat-num">'+(s.total_brands||0)+'</div><div class="stat-label">品牌数</div></div>';
  html+='<div class="stat-card"><div class="stat-num" style="color:'+dangerColor+'">'+(s.high_severity||0)+'</div><div class="stat-label">高严重度痛点</div></div>';
  html+='</div>';
  // 图表容器
  html+='<h3>📊 痛点标签分布 Top 10</h3><div id="rptChart1" style="width:100%;height:350px;margin-bottom:1.5rem;"></div>';
  html+='<h3>🥧 严重度分布</h3><div id="rptChart2" style="width:100%;height:300px;margin-bottom:1.5rem;"></div>';
  html+='<h3>🔥 品牌 × 痛点热力图</h3><div id="rptChart3" style="width:100%;height:400px;margin-bottom:1.5rem;"></div>';
  html+='<h3>🎯 优先级矩阵</h3><div id="rptChart4" style="width:100%;height:350px;margin-bottom:1.5rem;"></div>';
  html+='<hr style="border-color:'+gridColor+';margin:1.5rem 0;"><h3>📝 AI 改良建议详情</h3>';
  html+='<div class="report-text">'+reportHtml+'</div>';
  html+='</div>';
  document.getElementById('reportContent').innerHTML=html;
  // 渲染图表
  if(charts.tag_distribution){{
    var ch1=echarts.init(document.getElementById('rptChart1'));
    var td=charts.tag_distribution;
    ch1.setOption({{tooltip:{{trigger:'axis',axisPointer:{{type:'shadow'}}}},legend:{{data:['致命缺陷','影响体验','轻微吐槽'],textStyle:{{color:textColor}}}},grid:{{left:'8%',right:'5%',bottom:'15%'}},xAxis:{{type:'category',data:td.map(function(d){{return d.tag}}),axisLabel:{{color:textColor,rotate:30}},axisLine:{{lineStyle:{{color:gridColor}}}}}},yAxis:{{type:'value',axisLabel:{{color:textColor}},splitLine:{{lineStyle:{{color:gridColor}}}}}},series:[{{name:'致命缺陷',type:'bar',stack:'s',data:td.map(function(d){{return d.sev3||0}}),itemStyle:{{color:dangerColor}}}},{{name:'影响体验',type:'bar',stack:'s',data:td.map(function(d){{return d.sev2||0}}),itemStyle:{{color:'#fbbf24'}}}},{{name:'轻微吐槽',type:'bar',stack:'s',data:td.map(function(d){{return d.sev1||0}}),itemStyle:{{color:mutedColor}}}}]}});
  }}
  if(charts.severity_distribution){{
    var ch2=echarts.init(document.getElementById('rptChart2'));
    var sd=charts.severity_distribution;
    ch2.setOption({{tooltip:{{trigger:'item'}},legend:{{bottom:'0',textStyle:{{color:textColor}}}},series:[{{type:'pie',radius:['40%','70%'],data:sd.labels.map(function(l,i){{return{{name:l,value:sd.values[i]}}}}),itemStyle:{{borderColor:bgColor,borderWidth:2}},label:{{color:textColor}},color:[mutedColor,'#fbbf24',dangerColor]}}]}});
  }}
  if(charts.brand_tag_matrix){{
    var ch3=echarts.init(document.getElementById('rptChart3'));
    var bm=charts.brand_tag_matrix;
    var heatData=[];
    for(var i=0;i<bm.brands.length;i++){{for(var j=0;j<bm.tags.length;j++){{heatData.push([j,i,bm.data[i][j]]);}}}}
    ch3.setOption({{tooltip:{{position:'top'}},grid:{{left:'15%',right:'5%',bottom:'20%',top:'5%'}},xAxis:{{type:'category',data:bm.tags,axisLabel:{{color:textColor,rotate:30}},splitArea:{{show:false}}}},yAxis:{{type:'category',data:bm.brands,axisLabel:{{color:textColor}},splitArea:{{show:false}}}},visualMap:{{min:0,max:Math.max.apply(null,heatData.map(function(d){{return d[2]}}).concat([1])),calculable:true,orient:'horizontal',left:'center',bottom:'0',textStyle:{{color:textColor}},inRange:{{color:[bgColor,accentColor,dangerColor]}}}},series:[{{type:'heatmap',data:heatData,label:{{show:true,color:textColor}},emphasis:{{itemStyle:{{shadowBlur:10}}}}}}]}});
  }}
  if(charts.priority_matrix){{
    var ch4=echarts.init(document.getElementById('rptChart4'));
    var pm=charts.priority_matrix;
    ch4.setOption({{tooltip:{{formatter:function(p){{return p.data[2]+'<br/>频次: '+p.data[0]+'<br/>平均严重度: '+p.data[1]}}}},grid:{{left:'8%',right:'5%',bottom:'15%'}},xAxis:{{type:'value',name:'频次',nameTextStyle:{{color:textColor}},axisLabel:{{color:textColor}},splitLine:{{lineStyle:{{color:gridColor}}}}}},yAxis:{{type:'value',name:'平均严重度',nameTextStyle:{{color:textColor}},min:1,max:3,axisLabel:{{color:textColor}},splitLine:{{lineStyle:{{color:gridColor}}}}}},series:[{{type:'scatter',symbolSize:function(d){{return Math.sqrt(d[0])*4+10}},data:pm.map(function(d){{return[d.count,d.avg_severity,d.tag]}}),itemStyle:{{color:accentColor,opacity:0.7}},label:{{show:true,formatter:function(p){{return p.data[2]}},position:'top',color:textColor,fontSize:10}}}}]}});
  }}
}})();
</script>
</body>
</html>"""

    file_path = Path(export_dir) / safe_name
    file_path.write_text(final_html, encoding="utf-8")

    logger.info(f"报告已导出: {file_path}")
    return {"success": True, "path": str(file_path)}


@app.get("/api/export/read-file")
async def api_read_export_file(path: str):
    """读取已导出的 HTML 文件内容，供 PDF 打印使用"""
    try:
        file_path = Path(path)
        # 安全检查：只允许读取 .html 文件
        if file_path.suffix.lower() != ".html":
            raise HTTPException(status_code=400, detail="只允许读取 HTML 文件")
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")
        content = file_path.read_text(encoding="utf-8")
        return HTMLResponse(content=content)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("读取导出文件失败")
        raise HTTPException(status_code=500, detail=str(e))


# === 品牌管理 ===

@app.get("/api/brands")
async def api_brands():
    return get_all_brands()


@app.post("/api/brands")
async def api_add_brand(brand: BrandModel):
    try:
        if not brand.name.strip() or not brand.search_keyword.strip():
            raise ValueError("品牌名和搜索关键词不能为空")
        return add_brand(brand.name.strip(), brand.search_keyword.strip(), brand.brand_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/brands/{brand_id}")
async def api_update_brand(brand_id: str, brand: BrandModel):
    try:
        if not brand.name.strip() or not brand.search_keyword.strip():
            raise ValueError("品牌名和搜索关键词不能为空")
        update_brand(brand_id, brand.name.strip(), brand.search_keyword.strip(), brand.brand_type)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/brands/{brand_id}")
async def api_delete_brand(brand_id: str):
    delete_brand(brand_id)
    return {"status": "ok"}


@app.get("/api/analyses/{comment_id}")
async def api_get_analysis(comment_id: str):
    analysis = get_comment_analysis(comment_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="评论或分析结果不存在")
    return analysis


@app.put("/api/analyses/{comment_id}")
async def api_update_analysis(comment_id: str, payload: AnalysisUpdateModel):
    try:
        changed = update_analysis(comment_id, payload.model_dump(), human_corrected=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not changed:
        raise HTTPException(status_code=404, detail="分析结果不存在")
    return {"status": "ok", "analysis": get_comment_analysis(comment_id)}


# === 抓取 & 分析 ===

@app.post("/api/crawl")
async def api_crawl(
    brand_name: str = "",
    search_keyword: str = "",
    max_videos: int = 5,
    platform: str = "youtube",
):
    """创建后台抓取任务，立即返回 job_id；前端通过 /api/jobs/{id} 轮询。"""
    if platform not in {item["id"] for item in list_platforms()}:
        raise HTTPException(status_code=400, detail=f"不支持的平台: {platform}")
    max_videos = max(1, min(int(max_videos), 50))
    params = {
        "brand_name": brand_name.strip(),
        "search_keyword": search_keyword.strip(),
        "max_videos": max_videos,
        "platform": platform,
    }
    with _JOB_CREATE_LOCK:
        active = get_active_job("crawl", params)
        if active:
            return {"job_id": active["id"], "status": active["status"], "deduplicated": True}
        job_id = create_job("crawl", params)
    thread = threading.Thread(target=_run_crawl_job, args=(job_id, params), daemon=True)
    _JOB_THREADS[job_id] = thread
    thread.start()
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/platforms")
async def api_platforms():
    """返回支持的抓取平台列表（含配置状态）"""
    platforms = list_platforms()
    # 附加各平台的配置状态，前端据此提示用户
    ig_config = get_instagram_config()
    fb_config = get_facebook_config()
    reddit_cookie = get_reddit_cookie_config()
    reddit_oauth = get_reddit_config()
    reddit_configured = reddit_cookie["configured"] or reddit_oauth["configured"]
    reddit_hint = ""
    if not reddit_configured:
        reddit_hint = "Reddit 已封禁匿名访问，请在设置页面配置 Cookie 认证（推荐）或 OAuth"
    config_status = {
        "youtube": {"configured": True, "hint": ""},
        "reddit": {"configured": reddit_configured, "hint": reddit_hint},
        "instagram": {
            "configured": ig_config["configured"],
            "hint": "" if ig_config["configured"] else "请在设置页面配置 Instagram 账号",
        },
        "tiktok": {"configured": True, "hint": "TikTok 仅支持元数据抓取，无需配置"},
        "facebook": {
            "configured": fb_config["configured"],
            "hint": "" if fb_config["configured"] else "请在设置页面配置 Facebook Cookies",
        },
        "aliexpress": {"configured": True, "hint": ""},
    }
    for p in platforms:
        status = config_status.get(p["id"], {"configured": True, "hint": ""})
        p["config_status"] = status["configured"]
        p["config_hint"] = status["hint"]
    return platforms


# === 评论质量过滤（v2.0 W1-6）===

@app.get("/api/filter/config")
async def api_get_filter_config():
    return load_filter_config(refresh=True)


@app.post("/api/filter/config")
async def api_save_filter_config(config: dict):
    normalized = save_filter_config(config)
    return {"status": "ok", "config": normalized}


@app.post("/api/filter/recompute")
def api_filter_recompute():
    """按当前配置重算全量评论过滤标记（同步跑在线程池，避免阻塞事件循环）。"""
    stats = recompute_all_filters()
    return {"status": "ok", **stats}


# === 聚类依赖目录（桌面精简版外部接入）===

@app.get("/api/clustering/deps")
async def api_get_clustering_deps():
    from config import FROZEN
    from database import get_setting
    import clustering
    path = get_setting("clustering_deps_path", "")
    missing = clustering.missing_clustering_deps()
    return {"path": path, "missing": missing, "ready": not missing, "frozen": FROZEN}


@app.post("/api/clustering/deps/probe")
async def api_probe_clustering_deps(body: dict):
    """验证候选依赖目录（不保存，只测试）。"""
    import clustering
    return clustering.probe_external_deps(body.get("path", ""))


@app.post("/api/clustering/deps")
async def api_save_clustering_deps(body: dict):
    from database import set_setting
    import clustering
    path = (body.get("path", "") or "").strip()
    if not path:
        set_setting("clustering_deps_path", "")
        return {"status": "ok", "path": "", "message": "已清空聚类依赖目录"}
    probe = clustering.probe_external_deps(path)
    if not probe.get("ok"):
        return {"status": "error", "message": probe.get("message", "目录验证失败")}
    set_setting("clustering_deps_path", probe["path"])
    missing = clustering.missing_clustering_deps()
    return {
        "status": "ok",
        "path": probe["path"],
        "ready": not missing,
        "message": probe.get("message", "") + ("；重跑聚类已可用" if not missing else "；仍缺: " + "、".join(missing)),
    }


# === 痛点聚类（v2.0 Phase 2）===

class ClusterRunModel(BaseModel):
    mode: str = "full"            # full / incremental
    min_cluster_size: int = 10
    assign_threshold: float = 0.5


@app.post("/api/cluster/run")
async def api_cluster_run(payload: ClusterRunModel):
    """创建后台聚类任务。full=全量重跑（embedding 有缓存则秒级），incremental=新评论归簇，
    rename=仅对现有簇重新执行 LLM 命名（不重跑 embedding/聚类）。"""
    mode = payload.mode if payload.mode in ("full", "incremental", "rename") else "full"
    params = {
        "mode": mode,
        "min_cluster_size": max(5, min(int(payload.min_cluster_size), 200)),
        "assign_threshold": max(0.0, min(float(payload.assign_threshold), 1.0)),
    }
    with _JOB_CREATE_LOCK:
        active = get_active_job("cluster")
        if active:
            return {"job_id": active["id"], "status": active["status"], "deduplicated": True}
        job_id = create_job("cluster", params)
    thread = threading.Thread(target=_run_cluster_job, args=(job_id, params), daemon=True)
    _JOB_THREADS[job_id] = thread
    thread.start()
    return {"job_id": job_id, "status": "queued"}


def _run_cluster_job(job_id: str, params: dict):
    import clustering
    try:
        update_job(job_id, status="running", started_at=_now_iso(), message="正在初始化聚类")
        if is_job_cancel_requested(job_id):
            update_job(job_id, status="cancelled", message="任务已取消", completed_at=_now_iso())
            return

        def progress(current, total, message):
            update_job(job_id, progress_current=int(current), progress_total=int(total),
                       message=message)

        cancel = lambda: is_job_cancel_requested(job_id)
        if params.get("mode") == "incremental":
            result = clustering.run_incremental_clustering(
                threshold=params.get("assign_threshold"),
                progress_callback=progress, cancel_callback=cancel,
            )
        elif params.get("mode") == "rename":
            result = clustering.rename_active_clusters(progress_callback=progress)
        else:
            result = clustering.run_full_clustering(
                min_cluster_size=params.get("min_cluster_size"),
                progress_callback=progress, cancel_callback=cancel,
            )

        if is_job_cancel_requested(job_id):
            update_job(job_id, status="cancelled", message="任务已取消", completed_at=_now_iso())
            return
        if result.get("status") in ("empty", "no_active_clusters"):
            update_job(job_id, status="empty", progress_current=1, progress_total=1,
                       message=result.get("message", "无可聚类内容"),
                       result_json=json.dumps(result, ensure_ascii=False),
                       completed_at=_now_iso())
        elif result.get("status") == "failed":
            update_job(job_id, status="failed", progress_current=0, progress_total=1,
                       message=result.get("message", "聚类失败"),
                       result_json=json.dumps(result, ensure_ascii=False),
                       completed_at=_now_iso())
        else:
            if params.get("mode") == "incremental":
                msg = (f"增量聚类完成：新评论 {result['new_comments']} 条，"
                       f"归簇 {result['assigned']} 条，待全量重跑 {result['pending']} 条")
            elif params.get("mode") == "rename":
                msg = result.get("message", "重命名完成")
            else:
                msg = (f"聚类完成：{result['total']} 条评论 → {result['clusters']} 个主题"
                       f"（噪声 {result.get('noise', 0)} 条）")
            update_job(job_id, status="succeeded", progress_current=1, progress_total=1,
                       message=msg, result_json=json.dumps(result, ensure_ascii=False),
                       completed_at=_now_iso())
    except Exception as exc:
        logger.exception("聚类任务失败: %s", job_id)
        update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                   message="聚类失败", completed_at=_now_iso())
    finally:
        _JOB_THREADS.pop(job_id, None)


@app.get("/api/clusters")
async def api_clusters():
    """当前活跃版本的簇列表 + 待归簇新评论数。"""
    from database import get_clusters, get_active_cluster_version, get_clustered_comment_count
    clusters = get_clusters()
    version = get_active_cluster_version()
    clustered = get_clustered_comment_count()
    from database import get_db
    conn = get_db()
    pending = conn.execute(
        "SELECT COUNT(*) FROM comments WHERE cluster_id IS NULL AND filtered = 0 "
        "AND content_clean IS NOT NULL AND content_clean != ''"
    ).fetchone()[0]
    conn.close()
    return {
        "model_version": version,
        "clustered_comments": clustered,
        "pending_comments": pending,
        "clusters": clusters,
    }


@app.get("/api/clusters/{cluster_id}/comments")
async def api_cluster_comments(cluster_id: str, limit: int = 50, offset: int = 0):
    from database import get_cluster_comments
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return get_cluster_comments(cluster_id, limit=limit, offset=offset)


# === v2.0 Phase 3：建议报告（程序统计 + 证据链 + LLM 仅润色）===

class ReportRunModel(BaseModel):
    top_k: int = 15
    weight_reach: float = 0.35
    weight_negativity: float = 0.25
    weight_severity: float = 0.25
    weight_trend: float = 0.15


@app.post("/api/report/v2/generate")
async def api_report_v2_generate(payload: ReportRunModel):
    """生成 v2 建议报告（后台 job）：统计实算 → 优先级 → 机会点 → 建议（证据强制）。"""
    weights = {
        "reach": max(0.0, min(float(payload.weight_reach), 1.0)),
        "negativity": max(0.0, min(float(payload.weight_negativity), 1.0)),
        "severity": max(0.0, min(float(payload.weight_severity), 1.0)),
        "trend": max(0.0, min(float(payload.weight_trend), 1.0)),
    }
    params = {"top_k": max(5, min(int(payload.top_k), 50)), "weights": weights}
    with _JOB_CREATE_LOCK:
        active = get_active_job("report")
        if active:
            return {"job_id": active["id"], "status": active["status"], "deduplicated": True}
        job_id = create_job("report", params)
    thread = threading.Thread(target=_run_report_job, args=(job_id, params), daemon=True)
    _JOB_THREADS[job_id] = thread
    thread.start()
    return {"job_id": job_id, "status": "queued"}


def _run_report_job(job_id: str, params: dict):
    import advisor
    try:
        update_job(job_id, status="running", started_at=_now_iso(), message="正在计算聚类统计")
        if is_job_cancel_requested(job_id):
            update_job(job_id, status="cancelled", message="任务已取消", completed_at=_now_iso())
            return

        def progress(current, total, message):
            update_job(job_id, progress_current=int(current), progress_total=int(total),
                       message=message)

        result = advisor.assemble_report(
            weights=params.get("weights"), top_k=params.get("top_k"),
            progress_callback=progress,
        )

        if result.get("status") in ("no_clusters", "no_evidence"):
            update_job(job_id, status="empty", progress_current=1, progress_total=1,
                       message=result.get("message", "无法生成报告"),
                       result_json=json.dumps(result, ensure_ascii=False),
                       completed_at=_now_iso())
        elif result.get("status") == "failed":
            update_job(job_id, status="failed", progress_current=0, progress_total=1,
                       message=result.get("message", "报告生成失败"),
                       result_json=json.dumps(result, ensure_ascii=False),
                       completed_at=_now_iso())
        else:
            update_job(job_id, status="succeeded", progress_current=3, progress_total=3,
                       message=result.get("message", "报告生成完成"),
                       result_json=json.dumps(result, ensure_ascii=False),
                       completed_at=_now_iso())
    except Exception as exc:
        logger.exception("报告任务失败: %s", job_id)
        update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                   message="报告生成失败", completed_at=_now_iso())
    finally:
        _JOB_THREADS.pop(job_id, None)


@app.get("/api/report/v2/latest")
async def api_report_v2_latest():
    """最新一版报告（含建议列表，按优先级降序）。"""
    from database import get_latest_report
    report = get_latest_report()
    if not report:
        return {"exists": False}
    return {"exists": True, "report": report}


@app.get("/api/report/v2/history")
async def api_report_v2_history(limit: int = 20):
    from database import get_report_history
    return get_report_history(max(1, min(int(limit), 100)))


@app.get("/api/suggestions/{suggestion_id}/evidence")
async def api_suggestion_evidence(suggestion_id: str):
    """一条建议的证据评论（原文 + 译文 + 四元组 + 来源链接）。"""
    from database import get_suggestion_evidence_comment_ids, get_comments_by_ids
    ids = get_suggestion_evidence_comment_ids(suggestion_id)
    if not ids:
        raise HTTPException(status_code=404, detail="建议不存在或无证据")
    return get_comments_by_ids(ids)


@app.post("/api/comments/translate")
async def api_translate_comments(body: dict):
    """批量将评论翻译为中文（v1.2.2：证据链/簇内评论展示）。
    中文评论与已有全文翻译的评论自动跳过；结果永久缓存。"""
    from fastapi.concurrency import run_in_threadpool

    ids = body.get("comment_ids")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(status_code=400, detail="缺少 comment_ids")
    if len(ids) > 200:
        raise HTTPException(status_code=400, detail="单次最多翻译 200 条评论")

    config = get_llm_config()
    provider = config["provider"]
    if not provider:
        return {"status": "error", "message": "未配置 LLM 提供商，请先在设置页面配置"}

    from llm_provider import LLMClient
    from openai import AuthenticationError
    from translator import translate_comments

    try:
        client = LLMClient(
            provider=provider,
            api_key=config["api_keys"].get(provider, ""),
            model=config["models"].get(provider, ""),
        )
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    try:
        result = await run_in_threadpool(translate_comments, ids, client)
    except AuthenticationError:
        return {
            "status": "error",
            "message": f"{client.provider_name} 的 API Key 无效或已过期，请到设置页面重新配置",
        }
    result["status"] = "ok"
    return result


@app.get("/api/jobs")
async def api_jobs(limit: int = 20):
    return get_recent_jobs(limit)


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: str):
    job = request_job_cancel(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job["status"] not in {"queued", "running", "cancelled"}:
        raise HTTPException(status_code=409, detail="任务已经结束，无法取消")
    return job


def _run_crawl_job(job_id: str, params: dict):
    """在线程中执行抓取，主进程只负责 API 和任务状态。"""
    try:
        update_job(job_id, status="running", started_at=_now_iso(), message="正在初始化抓取")
        if is_job_cancel_requested(job_id):
            update_job(job_id, status="cancelled", message="任务已取消", completed_at=_now_iso())
            return
        brand_name = params.get("brand_name", "")
        search_keyword = params.get("search_keyword", "")
        max_videos = int(params.get("max_videos", 5))
        platform = params.get("platform", "youtube")

        if brand_name:
            if not search_keyword:
                brands = get_all_brands()
                b = next((x for x in brands if x["name"] == brand_name), None)
                search_keyword = b["search_keyword"] if b else brand_name
            result = crawl_competitor(
                brand_name, search_keyword, max_videos=max_videos, platform=platform,
                progress_callback=lambda current, total, message: update_job(
                    job_id, progress_current=current, progress_total=total, message=message
                ),
                cancel_callback=lambda: is_job_cancel_requested(job_id),
            )
            results = [result]
        else:
            brands = get_all_brands()
            if platform == "reddit":
                keyword = search_keyword or "rugged phone"
                results = crawl_all_brands_reddit(
                    brands, keyword, max_videos=max_videos,
                    progress_callback=lambda current, total, message: update_job(
                        job_id, progress_current=current, progress_total=total, message=message
                    ),
                    cancel_callback=lambda: is_job_cancel_requested(job_id),
                )
            else:
                results = []
                for index, brand in enumerate(brands, 1):
                    if is_job_cancel_requested(job_id):
                        update_job(job_id, status="cancelled", message="任务已取消", completed_at=_now_iso())
                        return
                    keyword = search_keyword or brand["search_keyword"] or brand["name"]
                    results.append(crawl_competitor(
                        brand["name"], keyword, max_videos=max_videos, platform=platform,
                        progress_callback=lambda current, total, message, idx=index: update_job(
                            job_id,
                            progress_current=(idx - 1) * 100 + round(current / max(total, 1) * 100),
                            progress_total=len(brands) * 100,
                            message=message,
                        ),
                        cancel_callback=lambda: is_job_cancel_requested(job_id),
                    ))
                    update_job(job_id, progress_current=index * 100, progress_total=len(brands) * 100,
                               message=f"已完成 {index}/{len(brands)} 个品牌")

        if is_job_cancel_requested(job_id):
            update_job(job_id, status="cancelled", message="任务已取消",
                       result_json=json.dumps({"results": results}, ensure_ascii=False), completed_at=_now_iso())
            return
        payload = {"results": results}
        # 0 条结果本身不一定是错误（例如关键词确实没有匹配），但任何网络/解析
        # 错误都必须让任务进入失败态，不能再显示“抓取完成 100%”的假成功。
        crawl_errors = []
        for item in results or []:
            if not isinstance(item, dict):
                continue
            if item.get("error"):
                crawl_errors.append(str(item["error"]))
            for detail in item.get("errors") or []:
                if isinstance(detail, dict):
                    stage = detail.get("stage", "")
                    message = detail.get("error", "")
                    crawl_errors.append(f"{stage}: {message}" if stage else str(message))
                else:
                    crawl_errors.append(str(detail))
        if crawl_errors:
            error_text = "；".join(dict.fromkeys(x for x in crawl_errors if x))[:2000]
            update_job(job_id, status="failed", progress_current=0, progress_total=1,
                       message="抓取失败（请查看错误详情）", error=error_text,
                       result_json=json.dumps(payload, ensure_ascii=False),
                       completed_at=_now_iso())
        elif not results or all(
            item.get("status") == "empty" or int(item.get("videos_found", 0) or 0) == 0
            for item in results if isinstance(item, dict)
        ):
            update_job(job_id, status="empty", progress_current=1, progress_total=1,
                       message="抓取完成，但未找到匹配内容",
                       result_json=json.dumps(payload, ensure_ascii=False),
                       completed_at=_now_iso())
        else:
            update_job(job_id, status="succeeded", progress_current=1, progress_total=1,
                       message="抓取完成", result_json=json.dumps(payload, ensure_ascii=False),
                       completed_at=_now_iso())
    except Exception as exc:
        logger.exception("抓取任务失败: %s", job_id)
        update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                   message="抓取失败", completed_at=_now_iso())
    finally:
        _JOB_THREADS.pop(job_id, None)


def _run_analyze_job(job_id: str, params: dict):
    try:
        update_job(job_id, status="running", started_at=_now_iso(), message="正在初始化 AI 分析")

        def progress(current, total, message):
            update_job(job_id, progress_current=current, progress_total=total, message=message)

        result = analyze_batch(
            limit=int(params.get("limit", 50)),
            brand=params.get("brand"),
            progress_callback=progress,
            cancel_callback=lambda: is_job_cancel_requested(job_id),
        )
        if result.get("cancelled"):
            status, message = "cancelled", "分析已取消"
        elif result.get("error"):
            status, message = "failed", "分析失败"
        else:
            status, message = "succeeded", "分析完成"
        update_job(job_id, status=status, progress_current=result.get("success", 0) + result.get("failed", 0),
                   progress_total=result.get("total", 0), message=message,
                   error=result.get("error"), result_json=json.dumps(result, ensure_ascii=False), completed_at=_now_iso())
    except Exception as exc:
        logger.exception("分析任务失败: %s", job_id)
        update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                   message="分析失败", completed_at=_now_iso())
    finally:
        _JOB_THREADS.pop(job_id, None)


@app.post("/api/analyze")
async def api_analyze(limit: int = 500, brand: str | None = None):
    """创建后台 AI 分析任务，立即返回 job_id。"""
    if str(limit) == "all" or limit == -1:
        limit = 999999
    else:
        limit = max(1, min(int(limit), 2000))
    params = {"limit": limit, "brand": brand}
    with _JOB_CREATE_LOCK:
        active = get_active_job("analyze", params)
        if active:
            return {"job_id": active["id"], "status": active["status"], "deduplicated": True}
        job_id = create_job("analyze", params)
    thread = threading.Thread(target=_run_analyze_job, args=(job_id, params), daemon=True)
    _JOB_THREADS[job_id] = thread
    thread.start()
    return {"job_id": job_id, "status": "queued"}


@app.post("/api/analyze/reanalyze")
async def api_reanalyze(brand: str | None = None):
    """重置旧分析记录并重新用新版 Prompt 分析。"""
    reset_count = reset_analyses_for_reanalysis(brand=brand)
    if reset_count == 0:
        return {"job_id": None, "status": "skipped", "message": "没有需要重新分析的评论"}
    limit = 999999
    params = {"limit": limit, "brand": brand}
    with _JOB_CREATE_LOCK:
        active = get_active_job("analyze", params)
        if active:
            return {"job_id": active["id"], "status": active["status"], "deduplicated": True, "reset_count": reset_count}
        job_id = create_job("analyze", params)
    thread = threading.Thread(target=_run_analyze_job, args=(job_id, params), daemon=True)
    _JOB_THREADS[job_id] = thread
    thread.start()
    return {"job_id": job_id, "status": "queued", "reset_count": reset_count}


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


@app.get("/api/insights/emotion-distribution")
async def api_emotion_distribution():
    """情绪类型分布（红帽维度）"""
    return get_emotion_distribution()


@app.get("/api/insights/positive-tags")
async def api_positive_tags():
    """正面反馈标签排行（黄帽维度）"""
    return get_positive_tags_distribution()


@app.get("/api/insights/user-solutions")
async def api_user_solutions():
    """用户改良方案汇总（绿帽维度）"""
    return get_user_solutions()


@app.get("/api/insights/sev3-by-brand")
async def api_sev3_by_brand():
    """致命缺陷按品牌分布（黑帽维度）"""
    return get_sev3_by_brand()


@app.get("/api/insights/solution-tags")
async def api_solution_tags():
    """用户改良方向 Top 10（绿帽维度）"""
    return get_solution_tags()


@app.get("/api/insights/analysis-progress")
async def api_analysis_progress():
    """AI 分析进度统计"""
    return get_analysis_progress()


@app.get("/api/insights/field-fill-rates")
async def api_field_fill_rates():
    """结构化字段填充率（蓝帽维度）"""
    return get_field_fill_rates()


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

### 四、新产品设计指导
基于以上痛点数据，为下一代三防手机产品提供设计指导：
1. **核心设计原则**：从痛点中提炼 3-5 条产品设计必须遵守的原则
2. **硬件规格建议**：针对屏幕、电池、防水、耐用性等维度给出具体规格建议
3. **软件体验优化**：系统流畅度、UI 交互、OTA 策略等改进方向
4. **场景化需求**：用户在哪些特殊场景下遇到问题，产品应如何覆盖
5. **差异化机会**：竞品普遍未做好的领域，可作为差异化卖点

### 五、改良优先级清单
综合频率×严重度×用户需求，输出 Top 10 改良优先级清单，格式：
| 优先级 | 痛点 | 严重度 | 频次 | 建议措施 | 预期收益 |
"""

        try:
            report = client.generate(prompt, temperature=0.3, max_tokens=4000, timeout=180.0)
            # 附带图表数据供前端渲染
            from database import get_stats as _get_stats
            _stats = _get_stats()
            charts = {
                "tag_distribution": summary["top_pains"][:10],
                "severity_distribution": get_severity_distribution(),
                "brand_tag_matrix": get_brand_tag_matrix(),
                "priority_matrix": get_priority_matrix()[:15],
                "stats": {
                    "total_comments": _stats.get("total_comments", 0),
                    "total_analyses": _stats.get("analyzed_comments", 0),
                    "total_brands": _stats.get("total_brands", 0),
                    "high_severity": _stats.get("high_severity", 0),
                },
            }
            return {"report": report, "generated_at": _get_now(), "charts": charts}
        except Exception as e:
            err_str = str(e)
            # 友好的认证错误提示
            if "401" in err_str or "AuthenticationError" in type(e).__name__ or "令牌" in err_str or "token" in err_str.lower():
                return {"error": f"LLM API Key 已过期或无效，请到设置页面更新 {config['provider'].upper()} 的 API Key"}
            if "429" in err_str or "RateLimitError" in type(e).__name__:
                from llm_provider import _rate_limit_message
                return {"error": _rate_limit_message(e, client.provider_name)}
            if "timeout" in err_str.lower() or "TimeoutError" in type(e).__name__:
                return {"error": "LLM 请求超时（已等待 180 秒），请检查网络连接或更换响应更快的 LLM 提供商（如 Gemini 或 DeepSeek）"}
            return {"error": f"生成失败: {type(e).__name__}: {e}"}

    return await run_in_threadpool(_generate)


def _get_now():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# === 竞品规格库 CRUD（F19）===

@app.post("/api/specs")
async def api_add_spec(spec: SpecModel):
    try:
        if not spec.brand_id.strip() or not spec.spec_category.strip() or not spec.spec_key.strip() or not spec.spec_value.strip():
            raise ValueError("品牌ID、参数类别、参数名和参数值不能为空")
        spec_id = add_spec(
            spec.brand_id.strip(), spec.spec_category.strip(), spec.spec_key.strip(),
            spec.spec_value.strip(), spec.spec_unit.strip(), spec.source_url.strip(),
            spec.model.strip(),
        )
        return {"status": "ok", "id": spec_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/specs")
async def api_get_specs(
    brand_id: str | None = None,
    category: str | None = None,
    key: str | None = None,
):
    return get_specs(brand_id=brand_id, category=category, key=key)


@app.put("/api/specs/{spec_id}")
async def api_update_spec(spec_id: str, spec: SpecUpdateModel):
    try:
        if not spec.spec_category.strip() or not spec.spec_key.strip() or not spec.spec_value.strip():
            raise ValueError("参数类别、参数名和参数值不能为空")
        changed = update_spec(
            spec_id, spec.spec_category.strip(), spec.spec_key.strip(),
            spec.spec_value.strip(), spec.spec_unit.strip(), spec.source_url.strip(),
            spec.model.strip(),
        )
        if not changed:
            raise HTTPException(status_code=404, detail="规格记录不存在")
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/specs/{spec_id}")
async def api_delete_spec(spec_id: str):
    delete_spec(spec_id)
    return {"status": "ok"}


# === Spec-Pain 回归分析（F17/F20）===

@app.get("/api/specs/regression")
async def api_spec_regression(spec_key: str, pain_tag: str):
    if not spec_key or not pain_tag:
        raise HTTPException(status_code=400, detail="spec_key 和 pain_tag 参数不能为空")
    return get_spec_regression(spec_key, pain_tag)


# === 四元组详情（F6/F18）===

@app.get("/api/analyses/{comment_id}/tuple")
async def api_get_tuple(comment_id: str):
    result = get_analysis_with_tuple(comment_id)
    if not result:
        raise HTTPException(status_code=404, detail="分析结果不存在")
    return result


# === 改良提案生成器（F21-F24）===

@app.post("/api/proposals/generate")
async def api_generate_proposal(req: ProposalRequestModel):
    """基于痛点聚类生成硬件规格改良提案"""
    from fastapi.concurrency import run_in_threadpool

    def _generate():
        if not req.pain_tag.strip():
            return {"error": "痛点标签不能为空"}

        pain_tag = req.pain_tag.strip()
        brand = req.brand.strip() if req.brand else None

        # F22: 提取 Top 3-5 条代表性评论（按点赞数+严重度排序）
        evidence_comments = get_top_pain_comments(
            pain_tag, brand=brand, min_severity=req.min_severity, limit=5
        )
        if not evidence_comments:
            return {"error": f"未找到痛点标签 '{pain_tag}' 的相关评论，请先抓取并分析数据"}

        # F23: 查询竞品规格库获取对应参数
        specs = get_specs(key=pain_tag)
        # 也查询与该痛点可能相关的规格键（如 weight → 重量）
        related_specs = get_specs()

        # 尝试获取回归分析数据
        regression = get_spec_regression(pain_tag, pain_tag)

        # 构建 LLM 输入
        import json as _json

        # 痛点证据链
        evidence_text = ""
        for i, c in enumerate(evidence_comments, 1):
            four_tuple = ""
            parts = []
            if c.get("context_environment"):
                parts.append(f"场景: {c['context_environment']}")
            if c.get("hardware_component"):
                parts.append(f"硬件: {c['hardware_component']}")
            if c.get("user_action"):
                parts.append(f"行为: {c['user_action']}")
            if c.get("pain_root_cause"):
                parts.append(f"根因: {c['pain_root_cause']}")
            four_tuple = " | ".join(parts)

            evidence_text += f"""
{i}. [{c.get('brand_name','未知')}] {c.get('product_match','')}
   原文: {c.get('content_clean','')[:200]}
   来源: {c.get('platform','')} - {c.get('video_title','')} ({c.get('source_url','')})
   四元组: {four_tuple}
   严重度: {c.get('severity','')} | 情感: {c.get('sentiment_score','')} | 情绪: {c.get('emotion_type','')}
   摘要: {c.get('summary_zh','')}
"""

        # 竞品规格对比
        specs_text = ""
        if specs:
            specs_text = "\n竞品规格数据:\n"
            for s in specs:
                specs_text += f"- {s.get('brand_name','')} {s.get('model','')}: {s.get('spec_key','')} = {s.get('spec_value','')} {s.get('spec_unit','')}\n"
        else:
            specs_text = "\n竞品规格库中暂无该参数的数据，请先在设置页录入竞品规格。\n"

        # 回归分析结论
        regression_text = ""
        if regression and not regression.get("error"):
            regression_text = f"\n回归分析: 临界值={regression.get('threshold','N/A')}\n"
            for dp in regression.get("data", []):
                regression_text += f"  {dp['brand']} {dp['model']}: x={dp['x']}{dp['unit']} → 负面占比={dp['y']}%\n"

        # BOM 与模具约束
        constraint_text = ""
        if req.bom_constraint:
            constraint_text += f"\nBOM 成本约束: {req.bom_constraint}\n"
        if req.mold_constraint:
            constraint_text += f"模具约束: {req.mold_constraint}\n"

        config = get_llm_config()
        provider = config["provider"]
        if not provider:
            return {"error": "未配置 LLM 提供商，请先在设置页面配置"}

        api_key = config["api_keys"].get(provider, "")
        model = config["models"].get(provider, "")
        try:
            client = LLMClient(provider=provider, api_key=api_key, model=model)
        except ValueError as e:
            return {"error": str(e)}

        prompt = f"""你是一名三防手机硬件产品总监，请基于以下用户痛点证据和竞品规格数据，生成一份《硬件规格改良提案》。

## 痛点证据链（代表性用户反馈）
{evidence_text}

## 竞品规格对标
{specs_text}
{regression_text}
{constraint_text}

## 输出要求（Markdown 格式）

### 一、痛点概述
- 痛点标签: {pain_tag}
- 涉及品牌与型号
- 问题严重度评估

### 二、用户原声证据链
列出上述 {len(evidence_comments)} 条代表性评论，每条包含：
- 用户原文摘要
- 来源链接
- 四元组分析（场景-硬件-行为-根因）

### 三、建议改良规格
基于竞品规格库和回归分析，给出具体参数建议：
- 当前规格（如有）→ 建议规格
- 对标竞品型号
- 改良理由（引用回归临界值或竞品对比数据）
- BOM 成本影响评估{'（需遵守约束: ' + req.bom_constraint + '）' if req.bom_constraint else ''}

### 四、工程验收用例
基于场景四元组自动生成测试标准，格式：
- 测试环境（温度/湿度/防护装备）
- 测试操作（具体动作与次数）
- 合格阈值（量化标准）

### 五、优先级与适用范围
- 改良优先级（P0/P1/P2）
- 适用于：下一代产品 / 当前产品 OTA 修复{'（模具约束: ' + req.mold_constraint + '）' if req.mold_constraint else ''}
- 预期收益（负面声量降低预估）
"""

        try:
            report = client.generate(prompt, temperature=0.3, max_tokens=4000, timeout=180.0)
            return {
                "report": report,
                "generated_at": _get_now(),
                "pain_tag": pain_tag,
                "brand": brand,
                "evidence_count": len(evidence_comments),
                "has_specs": bool(specs),
                "has_regression": not bool(regression.get("error")) if regression else False,
            }
        except Exception as e:
            err_str = str(e)
            if "401" in err_str or "AuthenticationError" in type(e).__name__:
                return {"error": f"LLM API Key 已过期或无效，请到设置页面更新 {config['provider'].upper()} 的 API Key"}
            if "429" in err_str or "RateLimitError" in type(e).__name__:
                from llm_provider import _rate_limit_message
                return {"error": _rate_limit_message(e, client.provider_name)}
            if "timeout" in err_str.lower() or "TimeoutError" in type(e).__name__:
                return {"error": "LLM 请求超时，请检查网络后重试"}
            return {"error": f"生成失败: {type(e).__name__}: {e}"}

    try:
        return await run_in_threadpool(_generate)
    except Exception as e:
        logger.exception("提案生成失败")
        return {"error": f"生成失败（内部错误 {type(e).__name__}）: {e}，请重试；若持续失败请检查数据或联系开发"}


# === 质量控制 — Gold Standard（F32）===

@app.post("/api/gold-standard")
async def api_add_gold_standard(gs: GoldStandardModel):
    try:
        if not gs.comment_id.strip():
            raise ValueError("comment_id 不能为空")
        gs_id = add_gold_standard(
            gs.comment_id.strip(), gs.expected_tags.strip(),
            gs.expected_severity, gs.expected_sentiment, gs.expected_four_tuple.strip(),
        )
        return {"status": "ok", "id": gs_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/gold-standard")
async def api_get_gold_standards():
    return get_gold_standards()


@app.delete("/api/gold-standard/{gs_id}")
async def api_delete_gold_standard(gs_id: str):
    delete_gold_standard(gs_id)
    return {"status": "ok"}


@app.get("/api/gold-standard/report")
async def api_gold_standard_report():
    """Gold Standard 跑分报告：比对 AI 输出与人工标注"""
    return get_gold_standard_report()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
