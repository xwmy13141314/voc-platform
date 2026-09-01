# -*- coding: utf-8 -*-
"""AI 改良建议生成引擎（v2.0 Phase 3 / W3）。

设计原则——"程序统计 + LLM 解释"分工防幻觉：
- 报告中的所有数字（聚类规模 / 负面占比 / 严重度均值 / 趋势 / 优先级分）全部由
  Python 从 SQLite 实算，LLM 拿到的是算好的事实表，只负责归纳与措辞
- 每条建议是一个结构化对象，evidence_comment_ids 必填且 ≥ 3，无证据不出建议
- LLM 不可用时全文模板降级（标题取聚类主题名、详情由统计拼装），报告仍完整可读

优先级公式（权重可在生成时调整）：
  priority = 0.35×影响面(簇规模归一) + 0.25×负面强度(簇内负向情感占比)
           + 0.25×严重度(簇内严重度均值归一) + 0.15×趋势因子(近90天声量增长)
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta

import database

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS = {"reach": 0.35, "negativity": 0.25, "severity": 0.25, "trend": 0.15}
DEFAULT_TOP_K = 15
MIN_EVIDENCE = 3                 # 每条建议最少证据评论数，不足则跳过该簇
EVIDENCE_POOL = 8                # 每条建议最多携带的证据数
OPPORTUNITY_MIN_BRANDS = 2       # 机会点：至少覆盖的竞品数
OPPORTUNITY_MIN_SIZE = 20        # 机会点：最小簇规模
OPPORTUNITY_POSITIVE_MAX = 0.2   # 机会点：簇内正面提及占比上限（超过视为已有竞品解决）

_EFFORT_BY_CATEGORY = {"hardware": "H", "software": "M", "scenario": "M", "ecosystem": "L"}


# ==================== 程序化统计层（W3-1）====================

def compute_cluster_statistics() -> list[dict]:
    """对活跃版本的每个簇实算统计事实表（全部数字来自 SQL，LLM 不参与计数）。"""
    version = database.get_active_cluster_version()
    clusters = database.get_clusters(version)
    if not clusters:
        return []

    conn = database.get_db()
    cutoff_recent = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    cutoff_prior = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")

    stats: list[dict] = []
    for c in clusters:
        rows = conn.execute("""
            SELECT c.id, c.platform, c.like_count, c.posted_at, c.crawled_at,
                   b.name AS brand_name,
                   a.severity, a.sentiment_score, a.pain_categories, a.pain_tags,
                   a.positive_tags, a.context_environment, a.user_action,
                   a.pain_root_cause, a.summary_zh
            FROM comments c
            LEFT JOIN brands b ON c.brand_id = b.id
            LEFT JOIN analyses a ON a.comment_id = c.id
            WHERE c.cluster_id = ? AND c.filtered = 0
        """, (c["id"],)).fetchall()
        conn.close()

        total = len(rows)
        analyzed = [r for r in rows if r["severity"] is not None]
        negative = [r for r in analyzed if (r["sentiment_score"] or 3) <= 2]
        sev3 = [r for r in analyzed if r["severity"] >= 3]

        # 时间趋势：近90天 vs 前90天（posted_at 缺失回退 crawled_at）
        recent = prior = 0
        for r in rows:
            ts = r["posted_at"] or r["crawled_at"] or ""
            if not ts:
                continue
            if ts >= cutoff_recent:
                recent += 1
            elif ts >= cutoff_prior:
                prior += 1
        growth = (recent - prior) / prior if prior > 0 else (1.0 if recent > 0 else 0.0)
        trend_factor = max(0.0, min(1.0, 0.5 + growth / 2))

        brands: dict[str, int] = {}
        brand_negative: dict[str, int] = {}
        brand_analyzed: dict[str, int] = {}
        platforms: dict[str, int] = {}
        categories: dict[str, int] = {}
        positive_mentions = 0
        for r in rows:
            bname = r["brand_name"] or "未知"
            brands[bname] = brands.get(bname, 0) + 1
            platforms[r["platform"] or "?"] = platforms.get(r["platform"] or "?", 0) + 1
            if r["severity"] is not None:
                brand_analyzed[bname] = brand_analyzed.get(bname, 0) + 1
                if (r["sentiment_score"] or 3) <= 2:
                    brand_negative[bname] = brand_negative.get(bname, 0) + 1
            for cat in _json_list(r["pain_categories"]):
                categories[cat] = categories.get(cat, 0) + 1
            if _json_list(r["positive_tags"]):
                positive_mentions += 1

        avg_sev = (sum(r["severity"] for r in analyzed) / len(analyzed)) if analyzed else None
        avg_sent = (sum(r["sentiment_score"] for r in analyzed) / len(analyzed)) if analyzed else None

        stats.append({
            "cluster": c,
            "cluster_id": c["id"],
            "topic_name": c.get("topic_name") or "",
            "topic_name_en": c.get("topic_name_en") or "",
            "keywords": c.get("keywords") or [],
            "description": c.get("description") or "",
            "size": total,
            "analyzed": len(analyzed),
            "negative_count": len(negative),
            "negative_ratio": (len(negative) / len(analyzed)) if analyzed else 0.0,
            "sev3_count": len(sev3),
            "avg_severity": round(avg_sev, 2) if avg_sev is not None else None,
            "avg_sentiment": round(avg_sent, 2) if avg_sent is not None else None,
            "brands": brands,
            "brand_count": len([b for b in brands if b != "未知"]),
            "brand_negative_ratio": {
                b: (brand_negative.get(b, 0) / brand_analyzed[b])
                for b in brand_analyzed if brand_analyzed[b] >= 3
            },
            "platforms": platforms,
            "categories": categories,
            "positive_ratio": (positive_mentions / total) if total else 0.0,
            "trend_factor": round(trend_factor, 3),
            "recent_90d": recent,
            "prior_90d": prior,
        })
        conn = database.get_db()
    conn.close()

    # 全局基线（用于机会点判定）
    total_analyzed = sum(s["analyzed"] for s in stats)
    total_negative = sum(s["negative_count"] for s in stats)
    global_negative_ratio = (total_negative / total_analyzed) if total_analyzed else 0.0
    max_size = max((s["size"] for s in stats), default=1) or 1
    for s in stats:
        s["reach"] = s["size"] / max_size
        s["severity_norm"] = (s["avg_severity"] / 3.0) if s["avg_severity"] else 0.0
        s["global_negative_ratio"] = round(global_negative_ratio, 3)
    return stats


def _json_list(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


# ==================== 优先级量化（W3-3）====================

def compute_priority(stat: dict, weights: dict | None = None) -> float:
    """优先级 0-10。权重和不必为 1，按比例归一。"""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    total_w = sum(w.values()) or 1.0
    score = (
        w["reach"] * stat["reach"]
        + w["negativity"] * stat["negative_ratio"]
        + w["severity"] * stat["severity_norm"]
        + w["trend"] * stat["trend_factor"]
    )
    return round(score / total_w * 10, 1)


def estimate_effort(stat: dict) -> str:
    """解决难度启发式：按簇内主导痛点分类映射 hardware=H / software,scenario=M / ecosystem=L。"""
    cats = stat.get("categories") or {}
    if not cats:
        return "M"
    top_cat = max(cats, key=cats.get)
    return _EFFORT_BY_CATEGORY.get(top_cat, "M")


def priority_factors_text(stat: dict, weights: dict | None = None) -> str:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    total_w = sum(w.values()) or 1.0
    return (f"影响面 {stat['size']}条/{round(stat['reach']*100)}%·权重{round(w['reach']/total_w*100)}%，"
            f"负面 {round(stat['negative_ratio']*100)}%·权重{round(w['negativity']/total_w*100)}%，"
            f"严重度均值 {stat['avg_severity'] or '—'}/3·权重{round(w['severity']/total_w*100)}%，"
            f"近90天 {stat['recent_90d']}条(前期{stat['prior_90d']}条)·权重{round(w['trend']/total_w*100)}%")


# ==================== 机会点挖掘（W3-4）====================

def detect_opportunities(stats: list[dict]) -> list[dict]:
    """程序化机会判定：声量达标 + 覆盖≥N竞品 + 各竞品差评率高于均值 + 正面回应缺失。"""
    opportunities = []
    for s in stats:
        if s["size"] < OPPORTUNITY_MIN_SIZE or s["brand_count"] < OPPORTUNITY_MIN_BRANDS:
            continue
        brand_ratios = s["brand_negative_ratio"]
        if len(brand_ratios) < OPPORTUNITY_MIN_BRANDS:
            continue
        if not all(r >= s["global_negative_ratio"] for r in brand_ratios.values()):
            continue
        if s["positive_ratio"] > OPPORTUNITY_POSITIVE_MAX:
            continue
        opportunities.append({
            "cluster_id": s["cluster_id"],
            "topic_name": s["topic_name"],
            "size": s["size"],
            "brands": sorted(s["brands"], key=s["brands"].get, reverse=True),
            "brand_negative_ratio": brand_ratios,
            "statement": _opportunity_statement(s),
        })
    opportunities.sort(key=lambda o: -o["size"])
    return opportunities


def _opportunity_statement(stat: dict) -> str:
    """机会陈述：当「情境」时，用户想要「动机」，但「障碍」——四元组字段程序拼装。"""
    conn = database.get_db()
    rows = conn.execute("""
        SELECT a.context_environment, a.user_action, a.pain_root_cause, a.summary_zh
        FROM comments c JOIN analyses a ON a.comment_id = c.id
        WHERE c.cluster_id = ? AND a.pain_root_cause IS NOT NULL
        ORDER BY a.severity DESC LIMIT 3
    """, (stat["cluster_id"],)).fetchall()
    conn.close()

    context = next((r["context_environment"] for r in rows if r["context_environment"]), None)
    action = next((r["user_action"] for r in rows if r["user_action"]), None)
    root = next((r["pain_root_cause"] for r in rows if r["pain_root_cause"]), None)
    summary = next((r["summary_zh"] for r in rows if r["summary_zh"]), None)

    topic = stat["topic_name"] or "该场景"
    situation = context or action or "用户使用三防手机的常见场景中"
    obstacle = root or summary or f"{topic} 相关痛点未被有效解决"
    return f"当「{situation}」时，用户想要「{topic}」相关的可靠体验，但「{obstacle}」——且覆盖 {stat['brand_count']} 个竞品品牌，均无有效正面回应，属差异化机会。"


# ==================== 证据链（W3-2）====================

def pick_evidence(cluster_id: str) -> list[dict]:
    """簇内证据评论：按 严重度×2 + 点赞×0.02 + 质量分 排序取前 EVIDENCE_POOL 条。"""
    conn = database.get_db()
    rows = conn.execute("""
        SELECT c.id, c.platform, c.content_clean, c.source_url, c.like_count, c.language,
               b.name AS brand_name,
               a.severity, a.sentiment_score, a.summary_zh, a.translation_zh, a.pain_tags,
               a.context_environment, a.user_action, a.pain_root_cause
        FROM comments c
        LEFT JOIN brands b ON c.brand_id = b.id
        LEFT JOIN analyses a ON a.comment_id = c.id
        WHERE c.cluster_id = ? AND c.filtered = 0
          AND c.content_clean IS NOT NULL AND c.content_clean != ''
        ORDER BY (COALESCE(a.severity,1) * 2 + COALESCE(c.like_count,0) * 0.02
                  + COALESCE(c.quality_score,0)) DESC
        LIMIT ?
    """, (cluster_id, EVIDENCE_POOL)).fetchall()
    conn.close()
    evidence = []
    for r in rows:
        e = dict(r)
        e["pain_tags"] = _json_list(e.get("pain_tags"))
        evidence.append(e)
    return evidence


# ==================== 建议生成器（W3-2）====================

SUGGESTION_PROMPT = """你是一名资深三防手机产品总监。以下是从多平台用户评论聚类得到的痛点主题簇及其程序实算统计数据（数字已由系统从数据库算好，直接引用，不要修改或臆造）。

请为每个簇输出一条产品改良建议，JSON 数组（顺序与输入一致）：
[
  {{
    "title": "15字以内的改良建议标题，动宾结构，如：提升极端低温下的电池可用容量",
    "detail_md": "120字以内 Markdown：现状（引用给定数字）→ 技术方向 → 预期收益",
    "spec_hint": "规格指向短语，如：低温电池方案 / 自加热电芯",
    "opportunity": "一句话机会陈述：当…时，用户想要…，但…"
  }}
]

只输出 JSON 数组，不要 markdown 代码块，不要解释。

{cluster_blocks}"""


def _suggestion_cluster_block(idx: int, s: dict, evidence: list[dict]) -> str:
    quotes = "\n".join(
        f"    - [{e.get('brand_name') or '?'}/{e.get('platform')}] "
        f"{(e.get('content_clean') or '')[:150]}"
        for e in evidence[:5]
    )
    return f"""### 簇 {idx}：{s['topic_name']}（{s['topic_name_en']}）
- 规模: {s['size']} 条评论（可聚类池内占比 {round(s['reach']*100)}%）
- 负面占比: {round(s['negative_ratio']*100)}%（{s['negative_count']}/{s['analyzed']} 条已分析）
- 严重度均值: {s['avg_severity'] or '—'}/3，其中致命(sev3) {s['sev3_count']} 条
- 近90天声量: {s['recent_90d']} 条（此前90天 {s['prior_90d']} 条）
- 覆盖品牌: {', '.join(f'{b}({n}条)' for b, n in sorted(s['brands'].items(), key=lambda x: -x[1])[:6])}
- 痛点分类: {json.dumps(s['categories'], ensure_ascii=False)}
- 关键词: {', '.join(s['keywords'][:6])}
- 代表评论:
{quotes}"""


def _polish_suggestions_via_llm(payloads: list[tuple[dict, list[dict]]]) -> list[dict | None]:
    """LLM 批量润色建议（仅措辞，不动数字）。失败位置返回 None 由模板降级。"""
    from analyzer import init_llm
    from openai import AuthenticationError

    results: list[dict | None] = [None] * len(payloads)
    try:
        client = init_llm()
    except ValueError as exc:
        logger.warning("LLM 未配置，建议生成降级为模板: %s", exc)
        return results

    batch_size = 5
    for start in range(0, len(payloads), batch_size):
        batch = payloads[start:start + batch_size]
        blocks = "\n\n".join(
            _suggestion_cluster_block(start + bi + 1, s, ev)
            for bi, (s, ev) in enumerate(batch)
        )
        prompt = SUGGESTION_PROMPT.format(cluster_blocks=blocks)
        for attempt in range(2):
            try:
                text = client.generate(prompt, temperature=0.3, max_tokens=3000)
                parsed = _parse_json_array(text)
                if parsed:
                    for bi in range(len(batch)):
                        results[start + bi] = parsed[bi] if bi < len(parsed) else None
                    break
                logger.warning("建议润色返回无法解析，重试 (%d/2)", attempt + 1)
            except AuthenticationError:
                logger.warning("LLM 认证失败，建议生成降级为模板")
                return results
            except Exception as exc:
                logger.warning("建议润色 LLM 调用失败: %s", exc)
                time.sleep(1)
    return results


def _parse_json_array(text: str) -> list[dict] | None:
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip().lstrip("\ufeff")
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [p for p in parsed if isinstance(p, dict)]
    except json.JSONDecodeError:
        pass
    return None


def _fallback_suggestion(s: dict, evidence: list[dict], priority: float,
                          effort: str) -> dict:
    """LLM 不可用时的模板建议：标题取主题名，详情由统计拼装（数字仍全部实算）。"""
    top_brands = sorted(s["brands"].items(), key=lambda x: -x[1])[:3]
    brands_text = "、".join(f"{b} {n}条" for b, n in top_brands) or "多品牌"
    sev_text = f"严重度均值 {s['avg_severity']}/3" if s["avg_severity"] else "暂无严重度数据"
    detail = (
        f"**现状**：{s['size']} 条评论反映「{s['topic_name']}」，负面占比 "
        f"{round(s['negative_ratio']*100)}%，{sev_text}，覆盖 {brands_text}。"
        f"近90天新增 {s['recent_90d']} 条相关声量。\n\n"
        f"**方向**：围绕关键词 {'、'.join(s['keywords'][:4])} 评估硬件与软件两侧的改进空间。\n\n"
        f"**依据**：下方 {len(evidence)} 条原始评论证据可展开核验。"
    )
    root = next((e.get("pain_root_cause") for e in evidence if e.get("pain_root_cause")), None)
    return {
        "title": f"改进：{s['topic_name'][:24]}",
        "detail_md": detail,
        "spec_hint": root or "、".join(s["keywords"][:3]),
        "opportunity": None,
    }


def generate_suggestions(stats: list[dict], weights: dict | None = None,
                         top_k: int = DEFAULT_TOP_K,
                         progress_callback=None) -> list[dict]:
    """按优先级取 Top-K 簇生成建议对象（证据强制 ≥ MIN_EVIDENCE 条）。"""
    ranked = sorted(stats, key=lambda s: -compute_priority(s, weights))[:top_k]

    payloads: list[tuple[dict, list[dict]]] = []
    for s in ranked:
        evidence = pick_evidence(s["cluster_id"])
        if len(evidence) < MIN_EVIDENCE:
            logger.info("簇 '%s' 证据不足 %d 条，跳过建议", s["topic_name"], len(evidence))
            continue
        payloads.append((s, evidence))

    if progress_callback:
        progress_callback(1, 3, f"AI 润色 {len(payloads)} 条建议（不可用时模板降级）...")
    polished = _polish_suggestions_via_llm(payloads)

    suggestions = []
    for idx, (s, evidence) in enumerate(payloads):
        llm = polished[idx]
        priority = compute_priority(s, weights)
        effort = estimate_effort(s)
        fb = llm or _fallback_suggestion(s, evidence, priority, effort)
        suggestions.append({
            "cluster_id": s["cluster_id"],
            "cluster_topic": s["topic_name"],
            "title": (llm.get("title") if llm else None) or fb["title"],
            "priority_score": priority,
            "priority_factors": priority_factors_text(s, weights),
            "evidence_comment_ids": [e["id"] for e in evidence],
            "evidence_quotes": [(e.get("content_clean") or "")[:120] for e in evidence[:4]],
            "evidence": evidence,
            "affected_brands": sorted(s["brands"].keys()),
            "spec_hint": (llm.get("spec_hint") if llm else None) or fb.get("spec_hint"),
            "effort": effort,
            "opportunity": (llm.get("opportunity") if llm else None) or fb.get("opportunity"),
            "detail_md": (llm.get("detail_md") if llm else None) or fb["detail_md"],
            "stats": {
                "size": s["size"], "negative_ratio": s["negative_ratio"],
                "avg_severity": s["avg_severity"], "sev3_count": s["sev3_count"],
                "recent_90d": s["recent_90d"], "brand_count": s["brand_count"],
            },
            "ai_polished": bool(llm),
        })
    return suggestions


# ==================== 报告组装与落库（W3-5）====================

def assemble_report(weights: dict | None = None, top_k: int = DEFAULT_TOP_K,
                    progress_callback=None) -> dict:
    """统计 → 优先级 → 机会点 → 建议 → 概览 Markdown → 落库。"""
    if progress_callback:
        progress_callback(0, 3, "计算聚类统计事实表 ...")
    stats = compute_cluster_statistics()
    if not stats:
        return {"status": "no_clusters",
                "message": "尚无聚类结果，请先在「痛点聚类」页执行全量聚类"}

    if progress_callback:
        progress_callback(1, 3, "计算优先级与机会点 ...")
    suggestions = generate_suggestions(stats, weights, top_k,
                                       progress_callback=progress_callback)
    opportunities = detect_opportunities(stats)

    if not suggestions:
        return {"status": "no_evidence",
                "message": "Top 簇内证据评论均不足 3 条，无法生成可溯源建议"}

    version = database.get_active_cluster_version()
    now = datetime.now()
    total_analyzed = sum(s["analyzed"] for s in stats)
    global_neg = round(sum(s["negative_count"] for s in stats) / total_analyzed * 100, 1) if total_analyzed else 0

    # 优先级队列：先改（高分低难）/ 排期（高分高难）/ 暂缓（低分）
    high = [s for s in suggestions if s["priority_score"] >= 5]
    do_now = [s for s in high if s["effort"] != "H"]
    schedule = [s for s in high if s["effort"] == "H"]
    defer = [s for s in suggestions if s["priority_score"] < 5]

    content_md = _overview_markdown(version, now, stats, suggestions,
                                    opportunities, global_neg, do_now, schedule, defer)

    if progress_callback:
        progress_callback(2, 3, "报告落库 ...")
    report_id = database.save_report({
        "title": f"痛点改良建议报告 · {now.strftime('%Y-%m-%d %H:%M')}",
        "params": {"weights": weights or DEFAULT_WEIGHTS, "top_k": top_k,
                   "cluster_version": version},
        "content_md": content_md,
        "model": "programmatic-v2" + ("+llm" if any(s["ai_polished"] for s in suggestions) else ""),
    })
    for s in suggestions:
        database.save_suggestion({**s, "report_id": report_id})

    result = {
        "status": "succeeded",
        "report_id": report_id,
        "cluster_version": version,
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "suggestions": len(suggestions),
        "ai_polished": sum(1 for s in suggestions if s["ai_polished"]),
        "opportunities": len(opportunities),
        "do_now": len(do_now), "schedule": len(schedule), "defer": len(defer),
        "message": (f"报告生成完成：{len(suggestions)} 条建议（AI 润色 "
                    f"{sum(1 for s in suggestions if s['ai_polished'])} 条），"
                    f"机会点 {len(opportunities)} 个"),
    }
    logger.info("v2 报告生成: %s", result["message"])
    return result


def _overview_markdown(version, now, stats, suggestions, opportunities,
                       global_neg, do_now, schedule, defer) -> str:
    """报告概览 Markdown：全局统计 + 优先级队列 + 机会点（数字全部实算）。"""
    total_clustered = sum(s["size"] for s in stats)
    lines = [
        f"# 痛点改良建议报告（v2 · 证据可溯源）",
        f"",
        f"> 聚类版本 `{version}` · 生成时间 {now.strftime('%Y-%m-%d %H:%M')} · "
        f"共 {len(stats)} 个主题簇 / {total_clustered} 条已聚类评论 · "
        f"全局负面占比 {global_neg}%",
        f"",
        f"## 优先级队列",
        f"",
        f"| 优先级 | 建议 | 得分 | 难度 | 证据数 | 覆盖品牌 |",
        f"|---|---|---|---|---|---|",
    ]
    rank = 0
    for s in suggestions:
        rank += 1
        lines.append(f"| {rank} | {s['title']} | {s['priority_score']} | {s['effort']} | "
                     f"{len(s['evidence_comment_ids'])} | {s['stats']['brand_count']} 个 |")

    lines += ["", f"### 建议执行顺序", f"- **先做**（高分·低/中难度）{len(do_now)} 条："
              + "、".join(s["title"] for s in do_now[:5])]
    if schedule:
        lines.append(f"- **排期**（高分·高难度）{len(schedule)} 条："
                     + "、".join(s["title"] for s in schedule[:5]))
    if defer:
        lines.append(f"- **暂缓**（低分）{len(defer)} 条")

    if opportunities:
        lines += ["", f"## 新产品机会点（程序判定 + {len(opportunities)} 个）", ""]
        for o in opportunities[:8]:
            lines.append(f"- **{o['topic_name']}**（{o['size']} 条声量，覆盖 "
                         f"{'、'.join(o['brands'][:4])}）：{o['statement']}")

    lines += ["", f"## 数据说明", "",
              f"- 所有统计数字由程序从 SQLite 实算，优先级 = 0.35×影响面 + 0.25×负面强度 "
              f"+ 0.25×严重度 + 0.15×趋势因子",
              f"- 每条建议携带 ≥{MIN_EVIDENCE} 条评论证据，可在前端展开核验原文与来源链接",
              f"- LLM 仅参与措辞润色，不参与计数（本次 AI 润色 "
              f"{sum(1 for s in suggestions if s['ai_polished'])}/{len(suggestions)} 条）"]
    return "\n".join(lines)
