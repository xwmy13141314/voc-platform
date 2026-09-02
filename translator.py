"""
评论中文翻译模块（v1.2.2）
批量 LLM 翻译外文评论为中文，结果写入 comment_translations 表永久缓存；
证据链 / 簇内评论展示用，翻译一次全站复用。
"""
import json
import logging
import re
import time

from llm_provider import LLMClient, _rate_limit_message
from openai import AuthenticationError, RateLimitError

logger = logging.getLogger(__name__)

BATCH_SIZE = 15

TRANSLATE_PROMPT = """将以下社交媒体用户评论逐条翻译成简体中文。

要求：
- 忠实原意，保留口语化表达与情绪（吐槽、讽刺、激动等）
- 品牌、型号、专有名词（如 Unihertz Tank、BlackBerry、QWERTY）保留英文
- 评论区常见缩写（IMO、tbh、smh 等）按对应中文语气翻译
- 每条评论独立翻译，不要合并、不要遗漏、不要新增评论

仅输出 JSON 数组，每项形如 {{"id": "评论ID原样返回", "zh": "中文翻译"}}，不要输出任何其他内容。

评论列表：
{items}"""


def is_chinese_text(text: str, threshold: float = 0.15) -> bool:
    """字母字符中 CJK 占比 ≥ 阈值即视为中文（容忍表情、@提及、URL）。"""
    if not text or not text.strip():
        return True
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return True  # 纯表情/数字/符号，翻译无意义
    cjk = sum(1 for ch in letters if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(letters) >= threshold


def _parse_translation_response(text: str, expect_ids: list[str]) -> dict[str, str]:
    """解析 LLM 返回的 JSON 数组 → {id: zh}；容错 markdown 包裹与前后噪声。"""
    if not isinstance(text, str) or not text.strip():
        return {}
    text = text.strip().lstrip("\ufeff")
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    valid = set(expect_ids)
    for candidate in candidates:
        try:
            arr = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(arr, list):
            continue
        out = {}
        for item in arr:
            if isinstance(item, dict) and item.get("id") in valid:
                zh = str(item.get("zh") or "").strip()
                if zh:
                    out[item["id"]] = zh
        if out:
            return out
    return {}


def translate_comments(
    comment_ids: list[str],
    client: LLMClient,
    progress_callback=None,
) -> dict:
    """批量翻译评论为中文。

    跳过：中文评论、已有全文翻译的评论（缓存表或 analyses.translation_zh）。
    结果写入 comment_translations 缓存。认证失败抛 AuthenticationError 由调用方处理。
    """
    from database import get_comments_by_ids, save_comment_translations

    unique_ids = list(dict.fromkeys(comment_ids))
    comments = get_comments_by_ids(unique_ids)
    if not comments:
        return {"total": 0, "translated": 0, "skipped": 0, "failed": 0, "batches": 0}

    pending = []
    skipped = 0
    for c in comments:
        content = (c.get("content_clean") or "").strip()
        if not content or is_chinese_text(content):
            skipped += 1
            continue
        if c.get("translation_source") in ("cache", "analysis"):
            skipped += 1  # 已有全文翻译，无需重翻
            continue
        pending.append(c)

    translated = 0
    failed = 0
    errors = []
    batches = (len(pending) + BATCH_SIZE - 1) // BATCH_SIZE

    for bi in range(batches):
        batch = pending[bi * BATCH_SIZE:(bi + 1) * BATCH_SIZE]
        ids = [c["id"] for c in batch]
        items = json.dumps(
            [{"id": c["id"], "text": (c.get("content_clean") or "")[:1500]} for c in batch],
            ensure_ascii=False,
        )
        prompt = TRANSLATE_PROMPT.format(items=items)
        got: dict[str, str] = {}
        for attempt in range(2):
            try:
                text = client.generate(prompt, temperature=0.1, max_tokens=3000)
                got = _parse_translation_response(text, ids)
                if got:
                    break
                logger.warning(
                    "翻译批次 %d/%d 返回无法解析，重试 (%d/2)", bi + 1, batches, attempt + 1
                )
            except AuthenticationError:
                raise
            except RateLimitError as exc:
                if attempt == 0:
                    time.sleep(3)
                    continue
                errors.append(_rate_limit_message(exc, client.provider_name))
                break
            except Exception as exc:
                logger.error("翻译批次调用失败: %s: %s", type(exc).__name__, exc)
                errors.append(f"{type(exc).__name__}: {exc}")
                break
        if got:
            save_comment_translations([(cid, zh, client.model) for cid, zh in got.items()])
            translated += len(got)
            failed += len(batch) - len(got)
        else:
            failed += len(batch)
        if progress_callback:
            progress_callback(min((bi + 1) * BATCH_SIZE, len(pending)), len(pending))

    result = {
        "total": len(comments),
        "translated": translated,
        "skipped": skipped,
        "failed": failed,
        "batches": batches,
    }
    if errors:
        result["errors"] = errors[:5]
    return result
