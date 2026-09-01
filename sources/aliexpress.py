"""AliExpress 电商评论抓取（v2.0 W1-2）。

走速卖通公开反馈接口 feedback.aliexpress.com/pc/searchEvaluation.do：
免登录、返回结构化 JSON、支持多语言评论与分页。评论以原语言入库
（俄/西/葡语为主），后续由多语言 embedding 统一处理，不依赖翻译。

输入模型：本平台不支持关键词搜索，"搜索"步骤的输入是商品链接 / 商品 ID
（支持逗号或换行分隔多个），自动解析 productId 后逐商品抓取评论。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

import requests

from .common import clean_comment, detect_language

logger = logging.getLogger(__name__)

_FEEDBACK_URL = "https://feedback.aliexpress.com/pc/searchEvaluation.do"
_PRODUCT_URL = "https://www.aliexpress.com/item/{product_id}.html"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://feedback.aliexpress.com/default/index/index.html",
}
_PAGE_SIZE = 20          # 接口单页上限保守值，实测各端差异较大
_PAGE_DELAY = 1.2        # 页间间隔，低频礼貌抓取
_MAX_PAGES = 50
_REQUEST_TIMEOUT = 20
_DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d %b %Y %H:%M", "%d %b %Y", "%Y.%m.%d")


class AliExpressError(RuntimeError):
    """AliExpress 抓取失败，向任务层传递用户可读的错误。"""


def parse_product_id(text: str) -> str | None:
    """从用户输入解析商品 ID。

    支持：
    - 完整链接   https://www.aliexpress.com/item/1005006123456789.html
    - 美国站链接 https://www.aliexpress.us/item/1005006123456789.html
    - 带 productId 参数的链接
    - 纯数字 ID（6-20 位）
    """
    text = (text or "").strip()
    if not text:
        return None
    m = re.search(r"/item/(\d{6,20})(?:\.html)?", text)
    if m:
        return m.group(1)
    m = re.search(r"[?&]productId=(\d{6,20})", text)
    if m:
        return m.group(1)
    m = re.fullmatch(r"\d{6,20}", text)
    if m:
        return m.group(0)
    return None


def search_products_aliexpress(keyword: str, limit: int = 10) -> list[dict]:
    """解析关键词中的商品链接/ID，返回商品条目（与平台注册表 search 契约一致）。"""
    tokens = [t for t in re.split(r"[,\n;，；\s]+", (keyword or "").strip()) if t]
    if not tokens:
        raise AliExpressError("请输入速卖通商品链接或商品 ID（可逗号分隔多个）")

    products, invalid = [], []
    for token in tokens:
        pid = parse_product_id(token)
        if not pid:
            invalid.append(token[:60])
            continue
        if any(p["video_id"] == pid for p in products):
            continue
        products.append({
            "video_id": pid,
            "external_id": pid,
            "title": f"AliExpress 商品 {pid}",
            "channel": "AliExpress",
            "view_count": 0,
            "comment_count": 0,
            "published_at": "",
            "source_url": _PRODUCT_URL.format(product_id=pid),
        })
        if len(products) >= max(1, limit):
            break

    if not products:
        hint = "" if len(invalid) != 1 else f"：{invalid[0]}"
        raise AliExpressError(
            "无法从输入解析出商品 ID" + hint +
            "。支持格式：https://www.aliexpress.com/item/100500xxxxx.html 或纯数字商品 ID"
            "（短链接 a.aliexpress.com 请先在浏览器打开后复制完整链接）"
        )
    if invalid:
        logger.warning("AliExpress 输入中 %d 项无法解析: %s", len(invalid), "; ".join(invalid[:3]))
    logger.info("AliExpress 解析出 %d 个商品", len(products))
    return products


def _fetch_page(product_id: str, page: int, lang: str = "en_US", country: str = "US") -> dict:
    params = {
        "productId": product_id,
        "lang": lang,
        "country": country,
        "pageSize": _PAGE_SIZE,
        "filter": "all",
        "sort": "complex_default",
        "page": page,
    }
    last_error = ""
    for attempt in range(3):
        try:
            resp = requests.get(_FEEDBACK_URL, params=params, headers=_HEADERS,
                                timeout=_REQUEST_TIMEOUT)
            if resp.status_code == 200:
                payload = resp.json()
                if payload.get("success") is False:
                    # 接口明确报错（常见：商品不存在 / 无评价权限）
                    msg = str(payload.get("errorMsg") or payload.get("error") or "接口返回失败")
                    raise AliExpressError(f"AliExpress 接口返回失败: {msg}")
                return payload
            last_error = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    raise AliExpressError(f"AliExpress 评论请求失败（第 {page} 页）: {last_error}")


def _iter_review_items(payload: dict) -> list[dict]:
    data = payload.get("data") or {}
    items = data.get("evaViewList") or data.get("evaluationResultList") or []
    return [i for i in items if isinstance(i, dict)]


def _parse_date(raw) -> str:
    raw = str(raw or "").strip()
    if not raw:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return raw  # 无法解析时保留原串，不阻断入库


def _comment_from_item(item: dict, product_id: str, page: int, index: int) -> dict | None:
    content = clean_comment(str(item.get("content") or ""))
    if not content or content in {"[Deleted]", "[deleted]"}:
        return None

    spec_info = item.get("specInfo") or []
    if isinstance(spec_info, dict):
        spec_info = spec_info.get("specInfos") or []
    variant = " / ".join(
        str(s.get("propertyValueName") or s.get("value") or "")
        for s in spec_info if isinstance(s, dict)
    ).strip(" /")

    images = item.get("images") or item.get("buyerGallery") or []
    review_id = str(item.get("id") or item.get("evaluationId") or f"{product_id}_p{page}i{index}")

    meta = {
        "rating": item.get("score") or item.get("rating"),
        "variant": variant or None,
        "country": item.get("country") or None,
        "has_images": bool(images),
    }
    return {
        "original_id": f"{product_id}_{review_id}",
        "content": content,
        "author": str(item.get("userName") or item.get("loginId") or "") or "Anonymous",
        "like_count": int(item.get("useful") or 0),
        "posted_at": _parse_date(item.get("buyDateStr") or item.get("formatDate") or item.get("gmtCreate")),
        "source_url": _PRODUCT_URL.format(product_id=product_id),
        "language": detect_language(content),
        "meta": {k: v for k, v in meta.items() if v is not None},
    }


def extract_comments_aliexpress(product_id: str, max_comments: int = 500) -> list[dict]:
    """抓取一个商品的全部评论（分页，直到 max_comments 或无更多）。"""
    comments: list[dict] = []
    seen_ids: set[str] = set()
    for page in range(1, _MAX_PAGES + 1):
        payload = _fetch_page(product_id, page)
        items = _iter_review_items(payload)
        if not items:
            break
        fresh = 0
        for index, item in enumerate(items):
            comment = _comment_from_item(item, product_id, page, index)
            if comment and comment["original_id"] not in seen_ids:
                seen_ids.add(comment["original_id"])
                comments.append(comment)
                fresh += 1
        if len(comments) >= max_comments or fresh == 0:
            break
        time.sleep(_PAGE_DELAY)
    logger.info("AliExpress 评论 %s -> %d 条", product_id, len(comments))
    return comments[:max_comments]
