"""
LLM 痛点分析模块
将非结构化评论转化为结构化痛点标签
支持多 LLM 提供商切换（Gemini/DeepSeek/GLM/Kimi/通义千问）
"""
import json
import logging
import re
import time
from database import get_unanalyzed_comments, insert_analysis, get_llm_config, normalize_analysis_result
from llm_provider import LLMClient
from openai import AuthenticationError, APITimeoutError, RateLimitError

logger = logging.getLogger(__name__)

# 分析 Prompt（角色设定 + 结构化输出约束）
PROMPT_VERSION = "v1.0"
ANALYSIS_PROMPT = """你是一名资深智能硬件产品经理，专注于三防手机赛道。你运用六顶思考帽分析法，从六个维度结构化拆解用户评论。

请分析以下用户评论，提取痛点信息。

## 评论信息
- 平台: {platform}
- 品牌: {brand}
- 语言: {language}
- 原文: {comment_text}

## 输出要求（仅输出 JSON，不要其他内容）
{{
  "sentiment_score": 1到5的整数,
  "pain_categories": ["hardware"或"software"或"scenario"或"ecosystem"之一或多个],
  "pain_tags": ["battery"或"screen"或"waterproof"或"system"或"weight"或"signal"或"camera"或"button"或"charging"或"durability"或"app_pairing"或"ota"或"ui"或"delay"之一或多个],
  "severity": 1到3的整数,
  "user_solution": "用户提出的改良建议原文，没有则null",
  "product_match": "评论针对的具体产品型号，无法判断则null",
  "translation_zh": "将评论准确翻译成中文；中文评论原样返回",
  "confidence": 0到1之间的小数，表示你对结构化判断的置信度,
  "summary_zh": "50字以内的中文摘要",
  "context_environment": "用户使用时的物理环境/气候/佩戴护具状况（如：降雨/低温-20°C/戴滑雪手套），无法判断则null",
  "hardware_component": "涉及的具体硬件元器件或结构设计（如：侧键/盲操凸起/电池防寒层），无法判断则null",
  "user_action": "用户当时的实际操作与交互行为（如：户外徒步中戴手套盲按侧键），无法判断则null",
  "pain_root_cause": "物理/工程层面的根本原因分析（如：按键行程0.5mm太短，缺乏明确触觉反馈），无法判断则null",
  "positive_tags": ["用户认可的功能或设计亮点，如'续航'或'防水'等关键词，没有则空数组"],
  "emotion_type": "anger或disappointment或satisfaction或surprise或neutral之一"
}}

说明:
- sentiment_score: 1=极度负面, 3=中性, 5=极度正面
- severity: 1=轻微吐槽, 2=影响体验, 3=致命缺陷
- pain_categories: hardware(硬件问题), software(软件/系统问题), scenario(特定场景失效), ecosystem(配件/生态问题)
- context_environment/hardware_component/user_action/pain_root_cause 构成"场景-硬件-行为-根因"四元组，将模糊抱怨还原为工程语言
- positive_tags: 用户主动提到的正面评价关键词
- emotion_type: 用户的主要情绪类型
- 只输出 JSON，不要 markdown 代码块，不要解释"""


def init_llm() -> LLMClient:
    """从数据库读取配置，初始化 LLM 客户端"""
    config = get_llm_config()
    provider = config["provider"]
    if not provider:
        raise ValueError(
            "未选择 LLM 提供商！请先在设置页面选择提供商并填入 API Key。"
        )
    api_key = config["api_keys"].get(provider, "")
    model = config["models"].get(provider, "")
    return LLMClient(provider=provider, api_key=api_key, model=model)


def parse_llm_response(text: str) -> dict | None:
    """解析 LLM 返回的 JSON，容错处理"""
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip().lstrip("\ufeff")
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start:end + 1])
    result = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                result = parsed
                break
        except json.JSONDecodeError:
            continue
    if result is None:
        logger.error(f"JSON 解析失败，原始文本: {text[:500]}")
        return None
    result["prompt_version"] = PROMPT_VERSION
    try:
        return normalize_analysis_result(result)
    except ValueError as exc:
        logger.warning(f"LLM 结构化结果校验失败: {exc}; payload={str(result)[:500]}")
        return None


def analyze_comment(comment: dict, client: LLMClient = None) -> dict | None:
    """分析单条评论。认证失败时抛出 AuthenticationError 由调用方处理。"""
    if client is None:
        client = init_llm()

    prompt = ANALYSIS_PROMPT.format(
        platform=comment.get("platform", "youtube"),
        brand=comment.get("brand_name", "未知"),
        language=comment.get("language", "en"),
        comment_text=comment.get("content_clean", comment.get("content", "")),
    )

    for attempt in range(2):
        try:
            request_prompt = prompt
            if attempt:
                request_prompt += "\n请修正上一次输出：只返回合法 JSON，枚举值必须严格遵守题目给出的列表。"
            text = client.generate(request_prompt, temperature=0.1, max_tokens=1200)
            result = parse_llm_response(text)
            if result:
                result["llm_model"] = client.model
                return result
            logger.warning(f"LLM 返回无法校验，准备重试 ({attempt + 1}/2)")
        except AuthenticationError:
            raise
        except (APITimeoutError, TimeoutError) as exc:
            logger.error(f"LLM 请求超时: {exc}")
            if attempt == 0:
                time.sleep(1)
                continue
            return None
        except RateLimitError as exc:
            logger.warning(f"LLM 限流: {exc}")
            if attempt == 0:
                time.sleep(2)
                continue
            return None
        except Exception as exc:
            logger.error(f"LLM 调用失败: {type(exc).__name__}: {exc}")
            return None
    return None


def analyze_batch(
    limit: int = 50,
    brand: str | None = None,
    progress_callback=None,
    cancel_callback=None,
) -> dict:
    """批量分析未处理的评论。认证失败时立即停止并返回错误。"""
    comments = get_unanalyzed_comments(limit=limit, brand=brand)

    if not comments:
        return {"total": 0, "success": 0, "failed": 0, "high_severity": 0}

    logger.info(f"开始分析 {len(comments)} 条评论...")

    try:
        client = init_llm()
    except ValueError as e:
        logger.error(str(e))
        return {"total": len(comments), "success": 0, "failed": len(comments),
                "error": str(e), "high_severity": 0}

    success = 0
    failed = 0
    high_severity = 0

    for i, comment in enumerate(comments, 1):
        if cancel_callback and cancel_callback():
            return {
                "total": len(comments), "success": success, "failed": failed,
                "high_severity": high_severity, "cancelled": True,
            }
        try:
            result = analyze_comment(comment, client=client)
        except AuthenticationError:
            # API Key 无效 — 立即停止，不再尝试后续评论
            error_msg = f"{client.provider_name} 的 API Key 无效或已过期，请到设置页面重新配置"
            logger.error(error_msg)
            return {
                "total": len(comments),
                "success": success,
                "failed": len(comments) - success,
                "high_severity": high_severity,
                "error": error_msg,
            }

        if result:
            try:
                insert_analysis(comment["id"], result, client.model)
                success += 1
                if result["severity"] == 3:
                    high_severity += 1
                logger.info(f"  [{i}/{len(comments)}] 成功 - 严重度{result['severity']} - {result['summary_zh'][:30]}")
            except ValueError as exc:
                failed += 1
                logger.warning(f"  [{i}/{len(comments)}] 结构化结果未写入: {exc}")
        else:
            failed += 1
            logger.warning(f"  [{i}/{len(comments)}] 失败 - {comment.get('content_clean', '')[:50]}")

        if progress_callback:
            progress_callback(i, len(comments), f"已处理 {i}/{len(comments)}")

    return {
        "total": len(comments),
        "success": success,
        "failed": failed,
        "high_severity": high_severity,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = analyze_batch(limit=5)
    print(json.dumps(result, indent=2, ensure_ascii=False))
