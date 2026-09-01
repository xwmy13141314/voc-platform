# -*- coding: utf-8 -*-
"""全局痛点聚类引擎（v2.0 Phase 2 / W2-1）。

管道：BGE-M3 多语言 embedding（本地 CPU 推理，向量缓存于 embeddings 表）
      → UMAP 降维（cosine → 10 维欧氏）
      → HDBSCAN 聚类（自动簇数，噪声点容忍）
      → LLM 主题命名（复用 analyzer 的 LLMClient，失败时降级占位名）
      → clusters 表落库 + comments.cluster_id 归属

设计要点：
- embedding 按模型版本缓存，全量重跑时只补算新增评论（分钟级 → 秒级）
- 增量模式：新评论向量与各簇质心比相似度，过阈值即归入，不重跑全量
- 旧聚类结果按 model_version 保留在 clusters 表中，可回溯
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime

try:
    import numpy as np
except ImportError:  # 桌面精简版未内置科学计算栈：簇查看/重命名可用，重跑聚类需源码模式
    np = None

import database

logger = logging.getLogger(__name__)

EMBEDDING_MODEL_NAME = "BAAI/bge-m3"
EMBEDDING_MODEL_VERSION = "bge-m3"          # embeddings 表的 model_version 键
EMBED_BATCH_SIZE = 32
EMBED_MAX_LENGTH = 128                       # 评论短文本，截断加速 CPU 推理
DEFAULT_MIN_CLUSTER_SIZE = 10
DEFAULT_ASSIGN_THRESHOLD = 0.5               # 增量归簇的质心余弦相似度阈值
REPRESENTATIVE_COUNT = 8                     # 每簇送 LLM 命名的代表评论数

_model = None
_model_lock = threading.Lock()


def _model_in_local_cache() -> bool:
    try:
        from huggingface_hub import scan_cache_dir
        return any(r.repo_id == EMBEDDING_MODEL_NAME for r in scan_cache_dir().repos)
    except Exception:
        return False


def _get_model():
    """懒加载 BGE-M3（首次约 10-30s，进程内复用）。评论短文本按 128 token 截断加速。

    模型已在本地缓存时强制 local_files_only——直连 huggingface.co 在部分网络下
    请求会无限挂起（WinError 10054 或 stall），而非快速失败。
    """
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        from sentence_transformers import SentenceTransformer
        local_only = _model_in_local_cache()
        if local_only:
            logger.info("检测到 %s 本地缓存，离线加载", EMBEDDING_MODEL_NAME)
        logger.info("加载 embedding 模型 %s ...", EMBEDDING_MODEL_NAME)
        started = time.time()
        _model = SentenceTransformer(EMBEDDING_MODEL_NAME, device="cpu",
                                     local_files_only=local_only)
        _model.max_seq_length = EMBED_MAX_LENGTH
        logger.info("embedding 模型就绪 (%.1fs, max_seq_length=%d)",
                    time.time() - started, _model.max_seq_length)
        return _model


def _embed_texts(texts: list[str]) -> np.ndarray:
    model = _get_model()
    vectors = model.encode(
        texts,
        batch_size=EMBED_BATCH_SIZE,
        show_progress_bar=False,
        normalize_embeddings=True,          # 归一化后点积即余弦相似度
    )
    return np.asarray(vectors, dtype=np.float32)


def ensure_embeddings(comments: list[dict], progress_callback=None,
                      cancel_callback=None) -> tuple[np.ndarray, list[str]]:
    """确保给定评论都有缓存向量，返回 (向量矩阵, comment_id 列表)。

    已缓存的直接从 embeddings 表读取，仅对缺矢量的评论做推理。
    """
    cached = database.load_embedding_map(EMBEDDING_MODEL_VERSION)
    ids: list[str] = []
    missing_idx: list[int] = []
    for i, c in enumerate(comments):
        cid = c["id"]
        if cid in cached:
            ids.append(cid)
        else:
            ids.append(cid)
            missing_idx.append(i)

    dim = None
    rows: list[np.ndarray] = [None] * len(comments)  # type: ignore
    for i, cid in enumerate(ids):
        if cid in cached:
            vec = np.frombuffer(cached[cid], dtype=np.float32)
            dim = dim or vec.shape[0]
            rows[i] = vec

    if missing_idx:
        logger.info("embedding 增量计算: %d 条（缓存命中 %d 条）",
                    len(missing_idx), len(comments) - len(missing_idx))
        batch_items: list[tuple[str, bytes, int]] = []
        chunk_start = 0
        for chunk in range(0, len(missing_idx), EMBED_BATCH_SIZE):
            if cancel_callback and cancel_callback():
                raise InterruptedError("embedding 被取消")
            idx_chunk = missing_idx[chunk:chunk + EMBED_BATCH_SIZE]
            texts = [comments[i]["content_clean"] for i in idx_chunk]
            vectors = _embed_texts(texts)
            for j, i in enumerate(idx_chunk):
                vec = vectors[j]
                rows[i] = vec
                batch_items.append((ids[i], vec.astype(np.float32).tobytes(), int(vec.shape[0])))
            if progress_callback:
                done = chunk_start + len(idx_chunk)
                progress_callback(
                    done, len(missing_idx),
                    f"embedding 计算中 {done}/{len(missing_idx)}",
                )
            chunk_start = done
        database.save_embeddings(EMBEDDING_MODEL_VERSION, batch_items)
    else:
        logger.info("embedding 全部命中缓存（%d 条）", len(comments))

    if any(r is None for r in rows):
        raise RuntimeError("存在缺失向量（不应到达此处）")
    dim = rows[0].shape[0]
    full = np.zeros((len(comments), dim), dtype=np.float32)
    for i, r in enumerate(rows):
        full[i] = r
    return full, ids


def _cluster_version_tag() -> str:
    return f"umap-hdbscan-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def missing_clustering_deps() -> list[str]:
    """重跑聚类所需的重量级依赖（桌面精简版可能未内置）。返回缺失包名列表。"""
    missing = []
    for module, package in (
        ("numpy", "numpy"),
        ("sentence_transformers", "sentence-transformers"),
        ("umap", "umap-learn"),
        ("hdbscan", "hdbscan"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


_DESKTOP_LITE_MESSAGE = (
    "桌面精简版未内置聚类依赖（torch / umap / hdbscan 约 1GB，不适合单文件打包）。"
    "当前版本仍可查看已有主题簇并执行「重新命名」；如需重跑聚类，"
    "请在源码模式安装 requirements.txt 中的聚类依赖后运行。"
)


def _representatives(comments: list[dict], member_idx: list[int]) -> list[dict]:
    """簇内按质量分挑代表评论。"""
    members = sorted(
        (comments[i] for i in member_idx),
        key=lambda c: (c.get("quality_score") or 0, c.get("like_count") or 0),
        reverse=True,
    )
    return members[:REPRESENTATIVE_COUNT]


NAMING_PROMPT = """你是一名资深智能硬件产品经理。以下是从多平台用户评论中聚类出的"痛点主题簇"。
每个簇附有若干代表性评论（保留原语言，未翻译）。

请为每个簇归纳命名，输出 JSON 数组（每个元素对应输入中的一个簇，顺序一致）：
[
  {{
    "topic_name": "10字以内的中文主题名，如：低温续航衰减",
    "topic_name_en": "英文短横线slug，如：battery-cold-performance",
    "description": "1-2句中文，概括该簇评论反映的共同痛点",
    "keywords": ["3-6个中英文关键词"]
  }}
]

只输出 JSON 数组，不要 markdown 代码块，不要解释。

{cluster_blocks}"""


def _naming_cluster_block(idx: int, rep: list[dict], stats: dict) -> str:
    lines = [f"### 簇 {idx}（{len(rep)} 条代表评论；平台分布: "
             f"{json.dumps(stats.get('platforms', {}), ensure_ascii=False)}）"]
    for r in rep:
        text = (r.get("content_clean") or "")[:200]
        lines.append(f"- [{r.get('platform', '')}/{r.get('brand_name', '')}] {text}")
    return "\n".join(lines)


def _parse_naming_response(text: str) -> list[dict] | None:
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


def _name_clusters_via_llm(cluster_payloads: list[tuple[list[dict], dict]]) -> list[dict | None]:
    """LLM 批量命名。返回与输入等长的列表，失败位置为 None（调用方降级）。"""
    from analyzer import init_llm
    from openai import AuthenticationError

    results: list[dict | None] = [None] * len(cluster_payloads)
    batch_size = 5
    try:
        client = init_llm()
    except ValueError as exc:
        logger.warning("LLM 未配置，簇命名降级为占位名: %s", exc)
        return results

    for start in range(0, len(cluster_payloads), batch_size):
        batch = cluster_payloads[start:start + batch_size]
        blocks = "\n\n".join(
            _naming_cluster_block(start + bi + 1, rep, stats)
            for bi, (rep, stats) in enumerate(batch)
        )
        prompt = NAMING_PROMPT.format(cluster_blocks=blocks)
        for attempt in range(2):
            try:
                text = client.generate(prompt, temperature=0.2, max_tokens=2000)
                parsed = _parse_naming_response(text)
                if parsed:
                    for bi in range(len(batch)):
                        results[start + bi] = parsed[bi] if bi < len(parsed) else None
                    break
                logger.warning("簇命名返回无法解析，重试 (%d/2)", attempt + 1)
            except AuthenticationError:
                logger.warning("LLM 认证失败，簇命名降级为占位名")
                return results
            except Exception as exc:
                logger.warning("簇命名 LLM 调用失败: %s", exc)
                time.sleep(1)
    return results


_FALLBACK_STOPWORDS = frozenset(
    "the this that and for with you your they them are was were have has had not but "
    "from what when will would could should about there their just like really very "
    "phone phones one all can get got its out how why who video review thanks thank "
    "don doesn isn aren wasn weren hasn haven won didn couldn shouldn wouldn gonna "
    "wanna yeah okay also because much many more some then than been being into know "
    "think need want use used using make made say said tell told look looking watch "
    "channel subscribe liked likes guy dude bro sure ever never always still even "
    "great good nice love best better worst awesome amazing pretty actually basically "
    "literally definitely probably maybe honestly too now after lol dont those either "
    "thing things stuff give going take which means does these keep content coming "
    "hope please follow kind thoughts sharing same another other most least next "
    "without within someone anyone everybody bought getting trying".split()
)


# 三防手机赛道领域词库：英文关键词 → 中文（降级命名用）。
# 未收录的词（品牌/型号名、专有名词）保留原文，保持"中文主题 + 英文品牌"的可读风格。
_GLOSSARY = {
    # 输入交互
    "physical keyboard": "实体键盘", "qwerty keyboard": "全键盘", "keyboard": "键盘",
    "touchscreen": "触屏", "touch screen": "触屏", "touch response": "触控响应",
    "buttons": "按键", "side buttons": "侧边按键", "power button": "电源键",
    "gloves": "手套操作", "glove mode": "手套模式",
    # 屏幕/相机
    "screen": "屏幕", "display": "显示屏", "screen brightness": "屏幕亮度",
    "dead pixel": "屏幕坏点", "screen protector": "贴膜",
    "camera": "相机", "camera quality": "成像质量", "photo quality": "成像质量",
    "night vision": "夜视功能", "night mode": "夜间模式", "night shots": "夜拍",
    "thermal imaging": "热成像", "thermal camera": "热成像相机", "thermal": "热成像",
    "infrared camera": "红外相机", "ir camera": "红外相机", "infrared": "红外",
    "low light": "弱光环境", "low light performance": "弱光表现",
    "photos videos": "照片与视频", "video quality": "视频质量", "photo": "照片",
    # 电池/性能
    "battery life": "续航", "battery": "电池", "battery drain": "耗电快",
    "battery capacity": "电池容量", "charging": "充电", "fast charging": "快充",
    "charger": "充电器", "performance": "性能", "processor": "处理器",
    "chipset": "芯片", "ram": "内存", "memory": "内存", "storage": "存储",
    "lag": "卡顿", "lagging": "卡顿", "slow": "运行缓慢", "overheating": "过热",
    "overheat": "过热", "heating": "发热", "crash": "死机", "boot loop": "无限重启",
    "restart": "重启", "bug": "软件缺陷", "bloatware": "预装软件",
    # 通信/传感器
    "signal": "信号", "reception": "信号接收", "call quality": "通话质量",
    "calls": "通话", "wifi": "Wi-Fi", "bluetooth": "蓝牙", "hotspot": "热点共享",
    "gps": "定位", "nfc": "NFC", "sim card": "SIM 卡", "dual sim": "双卡双待",
    "sd card": "存储卡", "microphone": "麦克风", "speaker": "扬声器",
    "sound quality": "音质", "headphone jack": "耳机接口", "flashlight": "手电筒",
    "walkie talkie": "对讲机", "fingerprint": "指纹识别", "fingerprint scanner": "指纹识别",
    "face unlock": "人脸解锁", "sensor": "传感器", "compass": "指南针",
    "barometer": "气压计",
    # 三防特性
    "rugged phone": "三防手机", "rugged": "三防", "waterproof": "防水",
    "water resistance": "防水性能", "water damage": "进水损坏",
    "drop test": "跌落测试", "drop protection": "防摔", "drop proof": "防摔",
    "durability": "耐用性", "build quality": "做工品质", "military standard": "军规标准",
    "ip68": "IP68 防护", "ip69": "IP69 防护", "dust": "防尘",
    # 外观/配件
    "design": "外观设计", "weight": "重量", "size": "尺寸", "grip": "握持感",
    "case": "保护壳",
    # 系统/售后
    "android": "安卓系统", "software update": "系统更新", "update": "系统更新",
    "security update": "安全更新", "security patch": "安全补丁", "firmware": "固件",
    "price": "价格", "value for money": "性价比", "shipping": "物流配送",
    "delivery": "到货速度", "warranty": "保修", "refund": "退款",
    "return policy": "退货政策", "customer service": "客服", "quality control": "品控",
    "defective": "瑕疵品", "scratch": "划痕", "scratches": "划痕",
    "scratching": "划痕", "broken": "损坏", "broke": "损坏",
    "stopped working": "突然失灵",
    # 投影
    "projector": "投影功能",
    # 户外/周边生态
    "power station": "户外电源", "solar panel": "太阳能板", "camping light": "露营灯",
    "solar charging": "太阳能充电", "wireless charging": "无线充电",
    "reverse charging": "反向充电", "charging port": "充电接口",
    "usb port": "USB 接口", "type c": "Type-C 接口", "otg": "OTG",
    "magnetic": "磁吸配件", "earbuds": "耳机", "earphone": "耳机",
    # 使用场景
    "cold weather": "低温环境", "underwater": "水下拍摄", "rain": "雨天使用",
    "wet hands": "湿手操作", "left swipe": "左滑操作", "swipe": "滑动手势",
    "screen size": "屏幕尺寸", "refresh rate": "刷新率", "gaming": "游戏性能",
    "games": "游戏", "benchmark": "跑分", "heat": "发热", "temperature": "温度",
    "durable": "耐用", "tough": "坚固", "customer support": "售后支持",
    "after sales": "售后", "production quality": "制作质量",
}


def _zh_keyword(kw: str) -> str:
    """词库翻译单个关键词；未收录的（品牌/型号名等）原样保留。"""
    return _GLOSSARY.get((kw or "").strip().lower(), kw)


def _extract_topic_keywords(texts: list[str], top_n: int = 5) -> list[str]:
    """LLM 不可用时的主题关键词提取：高频相邻词组优先，单词补充（过滤停用词）。"""
    from collections import Counter
    words: Counter = Counter()
    bigrams: Counter = Counter()
    for text in texts:
        tokens = [t for t in re.findall(r"[A-Za-z]{3,}|[\u4e00-\u9fff]{2,}", (text or "").lower())
                  if t not in _FALLBACK_STOPWORDS]
        words.update(tokens)
        for a, b in zip(tokens, tokens[1:]):
            if len(a) >= 4 and len(b) >= 4:
                bigrams[f"{a} {b}"] += 1
    ranked: list[str] = []
    for phrase, cnt in bigrams.most_common(10):
        if cnt >= 2:
            ranked.append(phrase)
    for w, _ in words.most_common(30):
        if w not in ranked and not any(w in p.split() for p in ranked):
            ranked.append(w)
    return ranked[:top_n]


def _fallback_name(index: int, comments: list[dict], stats: dict | None = None) -> dict:
    """LLM 不可用时的降级命名：关键词翻译成中文后拼装主题名（品牌/型号保留英文）。"""
    keywords = _extract_topic_keywords([(c.get("content_clean") or "") for c in comments])
    top = keywords[:3]
    if top:
        topic_name = " · ".join(_zh_keyword(k) for k in top)
        slug = "-".join(top).replace(" ", "-")[:60]
    else:
        topic_name, slug = f"主题 {index}", f"cluster-{index}"
    bits = []
    if stats:
        if stats.get("avg_severity") is not None:
            bits.append(f"平均严重度 {stats['avg_severity']}")
        if stats.get("avg_sentiment") is not None:
            bits.append(f"平均情感 {stats['avg_sentiment']}/5")
    desc = "关键词自动归纳" + (f"（{'，'.join(bits)}）" if bits else "")
    desc += "。配置 LLM API Key 后可点「重新命名」获得语义化主题。"
    return {
        "topic_name": topic_name,
        "topic_name_en": slug,
        "description": desc,
        "keywords": [_zh_keyword(k) for k in keywords],
    }


def _is_placeholder_name(cluster: dict) -> bool:
    """簇名是否仍是降级命名（主题 N / 关键词降级名 / 空），降级覆盖只针对这类簇。

    关键词降级名通过 description 中的固定标记识别；AI 语义名永远不会被降级覆盖。
    """
    name = (cluster.get("topic_name") or "").strip()
    slug = (cluster.get("topic_name_en") or "").strip()
    if not name or re.fullmatch(r"主题\s*\d+", name):
        return True
    if not slug or re.fullmatch(r"cluster-\d+", slug):
        return True
    if "关键词自动归纳" in (cluster.get("description") or ""):
        return True
    return False


def run_full_clustering(min_cluster_size: int | None = None,
                        progress_callback=None,
                        cancel_callback=None) -> dict:
    """全量聚类：embedding（增量缓存）→ UMAP → HDBSCAN → LLM 命名 → 落库。"""
    if missing_clustering_deps():
        return {"status": "failed", "message": _DESKTOP_LITE_MESSAGE}
    comments = database.get_clusterable_comments()
    total = len(comments)
    if total < max(min_cluster_size or DEFAULT_MIN_CLUSTER_SIZE, 20):
        return {"status": "empty", "total": total,
                "message": f"可聚类评论不足（{total} 条），先抓取并分析更多评论"}

    if progress_callback:
        progress_callback(0, total, "准备 embedding ...")
    matrix, ids = ensure_embeddings(
        comments,
        progress_callback=lambda cur, tot, msg: progress_callback(cur, tot, msg),
        cancel_callback=cancel_callback,
    )

    if progress_callback:
        progress_callback(total, total, "UMAP 降维 ...")
    import umap
    n_components = min(10, total - 1)
    reducer = umap.UMAP(
        n_components=n_components,
        n_neighbors=min(15, total - 1),
        min_dist=0.0,
        metric="cosine",
        random_state=42,
    )
    reduced = reducer.fit_transform(matrix)

    if progress_callback:
        progress_callback(total, total, "HDBSCAN 聚类 ...")
    import hdbscan
    mcs = min_cluster_size or DEFAULT_MIN_CLUSTER_SIZE
    mcs = max(5, min(mcs, total // 10))
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=mcs,
        metric="euclidean",
        cluster_selection_method="eom",
    )
    labels = clusterer.fit_predict(reduced)

    # 组织簇
    cluster_members: dict[int, list[int]] = {}
    for i, label in enumerate(labels):
        if label >= 0:
            cluster_members.setdefault(int(label), []).append(i)
    noise = int((labels < 0).sum())

    if not cluster_members:
        return {"status": "failed", "total": total, "clusters": 0, "noise": noise,
                "message": "HDBSCAN 未产生任何簇，尝试调小 min_cluster_size 后重跑"}

    # 统计 + 代表评论
    payloads: list[tuple[list[dict], dict]] = []
    member_sets: list[list[int]] = []
    for label in sorted(cluster_members, key=lambda l: -len(cluster_members[l])):
        member_idx = cluster_members[label]
        rep = _representatives(comments, member_idx)
        member_ids = [ids[i] for i in member_idx]
        stats = database.get_cluster_stats_for(member_ids)
        payloads.append((rep, stats))
        member_sets.append(member_idx)

    if progress_callback:
        progress_callback(total, total, f"LLM 命名 {len(payloads)} 个主题 ...")
    names = _name_clusters_via_llm(payloads)

    version = _cluster_version_tag()
    database.clear_cluster_assignments()
    assignments: dict[str, str] = {}
    summary_clusters: list[dict] = []
    for index, (member_idx, (rep, stats)) in enumerate(zip(member_sets, payloads)):
        member_ids = [ids[i] for i in member_idx]
        naming = names[index] or _fallback_name(
            index + 1, [comments[i] for i in member_idx], stats)
        cluster_id = database.save_cluster({
            "model_version": version,
            "topic_name": naming.get("topic_name") or f"主题 {index + 1}",
            "topic_name_en": naming.get("topic_name_en") or f"cluster-{index + 1}",
            "description": naming.get("description") or "",
            "keywords": naming.get("keywords") or [],
            "comment_count": len(member_ids),
            "representative_comment_ids": [r["id"] for r in rep],
            "avg_severity": stats.get("avg_severity"),
            "avg_sentiment": stats.get("avg_sentiment"),
        })
        for mid in member_ids:
            assignments[mid] = cluster_id
        summary_clusters.append({
            "cluster_id": cluster_id,
            "topic_name": naming.get("topic_name") or f"主题 {index + 1}",
            "comment_count": len(member_ids),
            "avg_severity": stats.get("avg_severity"),
        })
    database.assign_comments_to_clusters(assignments)
    database.set_active_cluster_version(version)

    result = {
        "status": "succeeded",
        "model_version": version,
        "total": total,
        "clusters": len(summary_clusters),
        "noise": noise,
        "clustered": total - noise,
        "cluster_list": summary_clusters,
    }
    logger.info("全量聚类完成: %d 条评论 -> %d 簇（噪声 %d）",
                total, len(summary_clusters), noise)
    return result


def run_incremental_clustering(threshold: float | None = None,
                               progress_callback=None,
                               cancel_callback=None) -> dict:
    """增量聚类：仅处理未归簇的新评论，与现有簇质心比对归入。

    阈值不满足的新评论保持未归簇状态，等待下次全量重跑。
    """
    if missing_clustering_deps():
        return {"status": "failed", "message": _DESKTOP_LITE_MESSAGE}
    version = database.get_active_cluster_version()
    clusters = database.get_clusters(version)
    if not clusters:
        return {"status": "no_active_clusters",
                "message": "尚无活跃聚类版本，请先执行全量聚类"}

    # 未归簇且可聚类的评论
    conn = database.get_db()
    rows = conn.execute("""
        SELECT id, content_clean, quality_score, like_count
        FROM comments
        WHERE cluster_id IS NULL AND filtered = 0
          AND content_clean IS NOT NULL AND content_clean != ''
        ORDER BY crawled_at
    """).fetchall()
    conn.close()
    new_comments = [dict(r) for r in rows]
    if not new_comments:
        return {"status": "empty", "assigned": 0, "pending": 0,
                "message": "没有待归簇的新评论"}

    if progress_callback:
        progress_callback(0, len(new_comments), "新评论 embedding ...")
    matrix, ids = ensure_embeddings(new_comments, progress_callback=progress_callback,
                                    cancel_callback=cancel_callback)

    # 各簇质心：从 embeddings 表 + comments.cluster_id 现算
    if progress_callback:
        progress_callback(len(new_comments), len(new_comments), "计算簇质心 ...")
    conn = database.get_db()
    center_rows = conn.execute("""
        SELECT c.cluster_id, e.vector
        FROM comments c
        JOIN embeddings e ON e.comment_id = c.id AND e.model_version = ?
        WHERE c.cluster_id IS NOT NULL
    """, (EMBEDDING_MODEL_VERSION,)).fetchall()
    conn.close()

    sums: dict[str, np.ndarray] = {}
    counts: dict[str, int] = {}
    for r in center_rows:
        vec = np.frombuffer(r["vector"], dtype=np.float32)
        cid = r["cluster_id"]
        sums[cid] = sums[cid] + vec if cid in sums else vec
        counts[cid] = counts.get(cid, 0) + 1
    centers = {cid: sums[cid] / counts[cid] for cid in sums}
    center_ids = list(centers.keys())
    center_matrix = np.vstack([centers[c] for c in center_ids])
    # 质心重新归一化后与归一化新向量点积 = 余弦相似度
    norms = np.linalg.norm(center_matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    center_matrix = center_matrix / norms

    thr = threshold if threshold is not None else DEFAULT_ASSIGN_THRESHOLD
    sim = matrix @ center_matrix.T                    # (new, clusters)
    best = sim.argmax(axis=1)
    best_score = sim.max(axis=1)

    assignments: dict[str, str] = {}
    per_cluster: dict[str, int] = {}
    for i, cid in enumerate(ids):
        if best_score[i] >= thr:
            target = center_ids[best[i]]
            assignments[cid] = target
            per_cluster[target] = per_cluster.get(target, 0) + 1

    # 更新簇规模
    name_by_id = {c["id"]: c.get("topic_name") or c["id"] for c in clusters}
    for cluster in clusters:
        added = per_cluster.get(cluster["id"], 0)
        if added:
            database.save_cluster({
                **cluster,
                "comment_count": cluster["comment_count"] + added,
                "representative_comment_ids": cluster.get("representative_comment_ids", []),
            })
    database.assign_comments_to_clusters(assignments)

    result = {
        "status": "succeeded",
        "model_version": version,
        "new_comments": len(new_comments),
        "assigned": len(assignments),
        "pending": len(new_comments) - len(assignments),
        "threshold": thr,
        "per_cluster": {name_by_id.get(cid, cid): n for cid, n in per_cluster.items()},
    }
    logger.info("增量聚类: %d 条新评论，归簇 %d，待全量重跑 %d",
                len(new_comments), len(assignments), len(new_comments) - len(assignments))
    return result


def rename_active_clusters(progress_callback=None) -> dict:
    """对当前活跃版本的全部簇重新命名（不重跑 embedding / 聚类）。

    LLM 可用时执行语义化命名；LLM 不可用（未配置 / Key 失效）时，
    对仍是占位名（"主题 N"）的簇降级为关键词命名，保证无 Key 时簇名也可读。
    已有 AI 语义名的簇不会被降级命名覆盖。
    """
    version = database.get_active_cluster_version()
    clusters = database.get_clusters(version)
    if not clusters:
        return {"status": "no_active_clusters", "message": "尚无活跃聚类版本，请先执行全量聚类"}

    payloads: list[tuple[list[dict], dict]] = []
    samples: list[list[dict]] = []
    for c in clusters:
        rep = database.get_cluster_comments(c["id"], limit=50)
        conn = database.get_db()
        member_ids = [r[0] for r in conn.execute(
            "SELECT id FROM comments WHERE cluster_id = ?", (c["id"],)
        ).fetchall()]
        conn.close()
        stats = database.get_cluster_stats_for(member_ids)
        payloads.append((rep[:REPRESENTATIVE_COUNT], stats))
        samples.append(rep)

    if progress_callback:
        progress_callback(0, len(payloads), f"重新命名 {len(payloads)} 个主题 ...")
    names = _name_clusters_via_llm(payloads)

    llm_named = fallback_named = 0
    for index, cluster in enumerate(clusters):
        naming = names[index]
        if naming:
            update = {
                "topic_name": naming.get("topic_name") or cluster["topic_name"],
                "topic_name_en": naming.get("topic_name_en") or cluster["topic_name_en"],
                "description": naming.get("description") or cluster.get("description") or "",
                "keywords": naming.get("keywords") or cluster.get("keywords") or [],
            }
            llm_named += 1
        elif _is_placeholder_name(cluster):
            update = _fallback_name(index + 1, samples[index], payloads[index][1])
            fallback_named += 1
        else:
            continue
        database.save_cluster({**cluster, **update})

    renamed = llm_named + fallback_named
    if renamed == 0:
        status, message = "failed", "LLM 命名失败，且现有主题名均无需更新"
    elif llm_named == 0:
        status = "succeeded"
        message = (f"LLM 不可用（请检查设置页 API Key），已按关键词降级命名 "
                   f"{fallback_named}/{len(clusters)} 个主题；配置 Key 后可再点「重新命名」升级为语义化主题")
    else:
        status = "succeeded"
        message = f"重命名完成：AI 命名 {llm_named} 个"
        if fallback_named:
            message += f"，关键词降级 {fallback_named} 个"
    result = {
        "model_version": version,
        "clusters": len(clusters),
        "renamed": renamed,
        "llm_named": llm_named,
        "fallback_named": fallback_named,
        "status": status,
        "message": message,
    }
    logger.info("簇重命名: AI %d / 关键词降级 %d / 共 %d（版本 %s）",
                llm_named, fallback_named, len(clusters), version)
    return result
