"""评论质量过滤引擎（v2.0 W1-6）。

在数据落盘前做一道可配置的规则闸门，从源头降低信噪比与 LLM 分析成本。
被过滤的评论不删除，仅标记 filtered=1 + filter_reason，阈值调整后可一键重算。

规则（全部可在设置页开关/调参）：
- min_words          有效长度下限（CJK 按字符、拉丁按词），低于即过滤
- emoji_only_filter  去表情后无实质内容 → 过滤
- link_only_filter   清洗（去 URL）后无实质内容 → 过滤
- link_spam_filter   链接垃圾（导购/带货评论）→ 过滤，满足其一即拦：
                    ① 原文含 URL 且清洗后残留文本 < link_spam_min_words（默认 8）
                    ② 原文含 URL 且命中促销话术（buy X here+URL / use code / % off /
                       official store / links to the best 等）
                    例："🛒 OUKITEL WP66 BUY HERE ➜ https://..." 残留 6 词 → ①拦；
                    "USE CODE OUK10 for 10% OFF! https://..." 残留 10 词 → ②拦
- min_likes          点赞数下限，按平台配置（0 = 关闭）；Reddit 默认 1（0/负分多为淹没评论）
- deep_reply_depth   楼中楼深度阈值：深度 >= 该值的评论不丢弃，只打 deep_reply 标记并扣质量分
                    （顶层评论 = 大众共性痛点，深层回复 = 极客细节探讨）

quality_score（0-1 启发式，供后续优先级/排序参考）：
0.5 基线 + 长度加成 + 点赞加成 - 深层回复扣分
"""
from __future__ import annotations

import json
import logging
import re

from .common import clean_comment, effective_length, has_emoji, strip_emoji

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

# 促销/带货话术（对清洗后文本匹配，需原文含 URL 才触发过滤）。
# 单纯长度阈值分不开"buy ... here + 链接"（残留可达 35 词）与真实用户长评论（18+ 词），
# 用话术模式精确打击导购评论。
_PROMO_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    # 导购句式：buy/get/grab/order ... here/below ... 紧跟 URL（对原始文本匹配，
    # here 与 URL 之间容忍少量分隔符/表情，如 "Here: 🔥 https://..."）
    r"(?:\bbuy|\bget|\bgrab|\border|\bpurchase)\b[\w\s]{2,40}?\b(?:here|below)\b[^\w\n]{0,8}https?://",
    r"\bbuy\s+(?:it|yours|the|this|now)\b",
    r"\bget\s+yours\b",
    r"\bgrab\s+(?:yours|the|your)\b",
    r"\bbuy\s+on\s+(?:aliexpress|shopify|amazon|ebay)\b",
    r"(?:discount|promo|coupon)\s+code",
    r"\buse\s+code\b",
    r"\bcode\s*[:：]\s*[a-z0-9]{3,}\b",
    r"\d+\s*%\s*off\b",
    r"\bspecial\s+discount\b",
    r"\bbest\s+(?:deal|price)\b",
    r"\bofficial\s+(?:store|website|site|shop)\b",
    r"\blinks?\s+to\s+the\s+best\b",
    r"\bwe\s+listed\s+in\s+this\s+video\b",
))
_ADJACENCY_RE = _PROMO_PATTERNS[0]  # 首条为邻接模式，需对原始文本（含 URL）匹配


def _has_promo_wording(text: str, raw: str) -> bool:
    """促销话术判定：普通模式匹配清洗文本，邻接模式匹配原始文本。"""
    for pattern in _PROMO_PATTERNS[1:]:
        if pattern.search(text):
            return True
    return bool(_ADJACENCY_RE.search(raw))

# 默认配置。存储于 settings 表 key=quality_filter_config（JSON）。
DEFAULT_CONFIG: dict = {
    "enabled": True,
    "min_words": 5,
    "emoji_only_filter": True,
    "link_only_filter": True,
    "link_spam_filter": True,
    "link_spam_min_words": 8,
    "min_likes": {"reddit": 1, "youtube": 0, "instagram": 0, "facebook": 0,
                  "tiktok": 0, "aliexpress": 0, "twitter": 0, "kickstarter": 0},
    "deep_reply_depth": 3,
    "deep_reply_penalty": 0.15,
}

_CONFIG_CACHE: dict | None = None


class Verdict:
    """单条评论的过滤判定结果。"""

    __slots__ = ("passed", "reason", "quality_score", "tags")

    def __init__(self, passed: bool, reason: str = "", quality_score: float = 0.5, tags: tuple = ()):
        self.passed = passed
        self.reason = reason
        self.quality_score = round(max(0.0, min(1.0, quality_score)), 3)
        self.tags = tags  # e.g. ("deep_reply",) 不阻断但留痕


def normalize_config(raw: dict | str | None) -> dict:
    """合并用户配置与默认值，容忍旧格式/缺字段。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = None
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if isinstance(raw, dict):
        for key in ("enabled", "min_words", "emoji_only_filter", "link_only_filter",
                    "link_spam_filter", "link_spam_min_words",
                    "deep_reply_depth", "deep_reply_penalty"):
            if key in raw and raw[key] is not None:
                cfg[key] = raw[key]
        if isinstance(raw.get("min_likes"), dict):
            merged = dict(cfg["min_likes"])
            merged.update({k: v for k, v in raw["min_likes"].items()})
            cfg["min_likes"] = merged
    try:
        cfg["min_words"] = max(0, int(cfg["min_words"]))
        cfg["link_spam_min_words"] = max(1, int(cfg["link_spam_min_words"]))
        cfg["deep_reply_depth"] = max(1, int(cfg["deep_reply_depth"]))
        cfg["deep_reply_penalty"] = max(0.0, min(1.0, float(cfg["deep_reply_penalty"])))
        cfg["min_likes"] = {k: max(0, int(v)) for k, v in cfg["min_likes"].items()}
    except (ValueError, TypeError):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return cfg


def load_filter_config(refresh: bool = False) -> dict:
    """从 settings 表读取质量过滤配置（带进程内缓存）。"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not refresh:
        return _CONFIG_CACHE
    try:
        from database import get_setting
        raw = get_setting("quality_filter_config", "")
        cfg = normalize_config(raw or None)
    except Exception as exc:
        logger.warning("加载质量过滤配置失败，使用默认值: %s", exc)
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    _CONFIG_CACHE = cfg
    return cfg


def save_filter_config(cfg: dict) -> dict:
    """保存配置到 settings 表并刷新缓存，返回归一化后的配置。"""
    from database import set_setting
    normalized = normalize_config(cfg)
    set_setting("quality_filter_config", json.dumps(normalized, ensure_ascii=False))
    global _CONFIG_CACHE
    _CONFIG_CACHE = normalized
    return normalized


def evaluate_comment(content_clean: str, platform: str, like_count: int,
                     depth: int = 0, cfg: dict | None = None,
                     raw_content: str | None = None) -> Verdict:
    """对单条评论做质量判定。

    content_clean: 已清洗文本（clean_comment 输出，URL 已移除）
    depth: 楼中楼深度，顶层评论为 0
    raw_content: 原始文本（含 URL）。link_spam 规则需要它判断"原文是否带链接"；
                 不传则跳过该规则（向后兼容）。
    """
    cfg = cfg or load_filter_config()
    text = (content_clean or "").strip()

    if not cfg.get("enabled"):
        return Verdict(True, "", 0.5)

    # 纯链接：原始内容有东西但清洗后为空 → link_only
    if not text:
        return Verdict(False, "link_only", 0.0)

    # 链接垃圾：原文带 URL 且（残留文本太少 或 命中促销话术）→ 导购/带货评论
    if cfg.get("link_spam_filter") and raw_content:
        if _URL_RE.search(raw_content) and (
            effective_length(text) < cfg["link_spam_min_words"]
            or _has_promo_wording(text, raw_content)
        ):
            return Verdict(False, "link_spam", 0.05)

    # 纯表情：仅在原文确实含 emoji 时检查，避免单词文本误归类为 emoji_only
    if cfg.get("emoji_only_filter") and has_emoji(text):
        residual = strip_emoji(text)
        if effective_length(residual) < 2:
            return Verdict(False, "emoji_only", 0.0)

    # 太短
    if effective_length(text) < cfg["min_words"]:
        return Verdict(False, "too_short", 0.05)

    # 点赞阈值（按平台，0 = 关闭）
    min_likes = int(cfg["min_likes"].get(platform, 0))
    if min_likes > 0 and (like_count or 0) < min_likes:
        return Verdict(False, "low_likes", 0.1)

    # ---- 通过：计算质量分 ----
    score = 0.5
    length = effective_length(text)
    if length >= 30:
        score += 0.25
    elif length >= 15:
        score += 0.15
    if (like_count or 0) >= 10:
        score += 0.2
    elif (like_count or 0) >= 5:
        score += 0.1
    tags = []
    if depth >= cfg["deep_reply_depth"]:
        score -= cfg["deep_reply_penalty"]
        tags.append("deep_reply")
    return Verdict(True, "", score, tuple(tags))


def recompute_all_filters() -> dict:
    """按当前配置重算全量评论的过滤状态（阈值调整后一键重算）。

    只更新质量字段，不动 content/analyzed 等业务字段。
    返回统计：{"total": N, "passed": N, "filtered": N, "by_reason": {...}}
    """
    import sqlite3

    import database

    cfg = load_filter_config(refresh=True)
    conn: sqlite3.Connection = database.get_db()
    try:
        rows = conn.execute(
            "SELECT id, platform, content, content_clean, like_count, depth FROM comments"
        ).fetchall()
        stats = {"total": 0, "passed": 0, "filtered": 0,
                 "by_reason": {"too_short": 0, "emoji_only": 0, "link_only": 0,
                               "link_spam": 0, "low_likes": 0}}
        updates: list[tuple] = []
        for row in rows:
            verdict = evaluate_comment(row["content_clean"] or "", row["platform"] or "",
                                       row["like_count"] or 0, row["depth"] or 0, cfg=cfg,
                                       raw_content=row["content"] or "")
            stats["total"] += 1
            if verdict.passed:
                stats["passed"] += 1
            else:
                stats["filtered"] += 1
                if verdict.reason in stats["by_reason"]:
                    stats["by_reason"][verdict.reason] += 1
            updates.append((0 if verdict.passed else 1, verdict.reason or None,
                            verdict.quality_score, row["id"]))
        conn.executemany(
            "UPDATE comments SET filtered = ?, filter_reason = ?, quality_score = ? WHERE id = ?",
            updates,
        )
        conn.commit()
        return stats
    finally:
        conn.close()
