"""
统一 LLM 提供商抽象层
支持 Gemini / DeepSeek / GLM(智谱) / Kimi(月之暗面) / 通义千问(阿里)
全部走 OpenAI 兼容接口，只需切换 base_url 和 api_key
"""
import logging
from openai import OpenAI, AuthenticationError, APITimeoutError, RateLimitError

logger = logging.getLogger(__name__)

# 请求超时（秒）— 避免无响应时长时间挂起
REQUEST_TIMEOUT = 60.0

# 提供商配置表
PROVIDERS = {
    "gemini": {
        "name": "Gemini (Google)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.5-flash",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash"],
        "key_url": "https://aistudio.google.com/apikey",
    },
    "deepseek": {
        "name": "DeepSeek (深度求索)",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_url": "https://platform.deepseek.com/api_keys",
    },
    "glm": {
        "name": "GLM (智谱清言)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4", "glm-4-air"],
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
    },
    "kimi": {
        "name": "Kimi (月之暗面)",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "key_url": "https://platform.moonshot.cn/console/api-keys",
    },
    "qwen": {
        "name": "通义千问 (阿里)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-turbo",
        "models": ["qwen-turbo", "qwen-plus", "qwen-max"],
        "key_url": "https://dashscope.console.aliyun.com/apiKey",
    },
}


class LLMClient:
    """统一 LLM 客户端 — 屏蔽各提供商差异"""

    def __init__(self, provider: str, api_key: str, model: str = ""):
        self.provider = provider
        config = PROVIDERS.get(provider)
        if not config:
            raise ValueError(f"未知的 LLM 提供商: {provider}，可选: {list(PROVIDERS.keys())}")

        if not api_key:
            raise ValueError(f"{config['name']} 的 API Key 未设置，请先在设置页面配置")

        self.base_url = config["base_url"]
        self.model = model or config["default_model"]
        self.provider_name = config["name"]
        # 超时 60s + 禁用自动重试，避免无效 Key 时重试 3 次卡住 3 分钟
        self.client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=REQUEST_TIMEOUT,
            max_retries=0,
        )
        logger.info(f"LLM 客户端就绪: {self.provider_name} / model={self.model}")

    def generate(self, prompt: str, temperature: float = 0.1, max_tokens: int = 500) -> str:
        """调用 LLM 生成文本，返回原始响应字符串"""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是一名资深智能硬件产品经理，专注于三防手机赛道。请严格按照要求输出，不要多余解释。"},
                {"role": "user", "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content

    def test_connection(self) -> tuple[bool, str]:
        """发送一条测试请求，返回 (是否成功, 消息)"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "请回复：连接测试成功"}],
                max_tokens=20,
                temperature=0,
            )
            reply = response.choices[0].message.content.strip()
            return True, f"连接成功，模型回复: {reply}"
        except AuthenticationError:
            return False, f"认证失败：{self.provider_name} 的 API Key 无效或已过期，请重新获取并配置"
        except APITimeoutError:
            return False, f"请求超时：{self.provider_name} 服务器未在 {REQUEST_TIMEOUT}s 内响应，请检查网络或更换提供商"
        except RateLimitError:
            return False, f"请求被限流：{self.provider_name} 的 API 调用频率超限，请稍后重试"
        except Exception as e:
            return False, f"连接失败: {type(e).__name__}: {e}"


def get_provider_info(provider: str) -> dict:
    """获取提供商配置信息"""
    return PROVIDERS.get(provider, {})


def list_providers() -> list[dict]:
    """列出所有可用提供商"""
    return [
        {"id": pid, "name": p["name"], "default_model": p["default_model"],
         "models": p["models"], "key_url": p["key_url"]}
        for pid, p in PROVIDERS.items()
    ]
