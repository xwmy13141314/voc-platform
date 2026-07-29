"""
LLM 痛点分析模块
将非结构化评论转化为结构化痛点标签
支持多 LLM 提供商切换（Gemini/DeepSeek/GLM/Kimi/通义千问）
"""
import json
import logging
import re
from database import get_unanalyzed_comments, insert_analysis, get_llm_config
from llm_provider import LLMClient
from openai import AuthenticationError, APITimeoutError, RateLimitError

logger = logging.getLogger(__name__)

# 分析 Prompt（角色设定 + 结构化输出约束）
ANALYSIS_PROMPT = """你是一名资深智能硬件产品经理，专注于三防手机赛道。

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
  "summary_zh": "50字以内的中文摘要"
}}

说明:
- sentiment_score: 1=极度负面, 3=中性, 5=极度正面
- severity: 1=轻微吐槽, 2=影响体验, 3=致命缺陷
- pain_categories: hardware(硬件问题), software(软件/系统问题), scenario(特定场景失效), ecosystem(配件/生态问题)
- pain_tags: 具体痛点维度标签
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
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        result = json.loads(text)
        required = ["sentiment_score", "pain_categories", "pain_tags",
                     "severity", "summary_zh"]
        for field in required:
            if field not in result:
                logger.warning(f"LLM 返回缺少字段: {field}")
                return None

        result["sentiment_score"] = int(result["sentiment_score"])
        result["severity"] = int(result["severity"])
        result["sentiment_score"] = max(1, min(5, result["sentiment_score"]))
        result["severity"] = max(1, min(3, result["severity"]))

        if not isinstance(result["pain_categories"], list):
            result["pain_categories"] = [result["pain_categories"]] if result["pain_categories"] else []
        if not isinstance(result["pain_tags"], list):
            result["pain_tags"] = [result["pain_tags"]] if result["pain_tags"] else []

        return result
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {e}\n原始文本: {text[:200]}")
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

    try:
        text = client.generate(prompt, temperature=0.1, max_tokens=500)
        result = parse_llm_response(text)
        if result:
            result["llm_model"] = client.model
        return result
    except AuthenticationError:
        # 认证失败 — 向上抛出，让批量分析立即停止
        raise
    except (APITimeoutError, TimeoutError) as e:
        logger.error(f"LLM 请求超时: {e}")
        return None
    except Exception as e:
        logger.error(f"LLM 调用失败: {type(e).__name__}: {e}")
        return None


def analyze_batch(limit: int = 50, brand: str | None = None) -> dict:
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
            insert_analysis(comment["id"], result, client.model)
            success += 1
            if result["severity"] == 3:
                high_severity += 1
            logger.info(f"  [{i}/{len(comments)}] 成功 - 严重度{result['severity']} - {result['summary_zh'][:30]}")
        else:
            failed += 1
            logger.warning(f"  [{i}/{len(comments)}] 失败 - {comment.get('content_clean', '')[:50]}")

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
