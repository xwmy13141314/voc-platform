"""共享文本工具。crawler.py 与 sources/ 各平台模块共用，单一实现避免漂移。"""
import re

# CJK 判定：含中日韩字符的文本按"字符数"而非"词数"衡量长度
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # symbols & pictographs, supplemental
    "\U00002700-\U000027BF"   # dingbats
    "\U0001F000-\U0001F02F"   # mahjong etc.
    "\U00002600-\U000026FF"   # misc symbols
    "\U0000FE0F"              # variation selector
    "\U00002B00-\U00002BFF"   # arrows/stars
    "\U00002190-\U000021FF"   # arrows
    "]+",
    flags=re.UNICODE,
)


def clean_comment(text: str) -> str:
    """清洗评论文本：去 URL / @提及 / HTML 标签，压缩空白。"""
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def detect_language(text: str) -> str:
    """检测评论语言，失败时回退 en。"""
    try:
        from langdetect import detect
        return detect(text)
    except Exception:
        return "en"


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def effective_length(text: str) -> int:
    """文本有效长度：CJK 文本按字符数，其他按词数。用于质量过滤的统一度量。"""
    text = (text or "").strip()
    if has_cjk(text):
        return len(re.sub(r"\s", "", text))
    return len([w for w in text.split() if w])


def strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text or "")


def has_emoji(text: str) -> bool:
    return bool(_EMOJI_RE.search(text or ""))
