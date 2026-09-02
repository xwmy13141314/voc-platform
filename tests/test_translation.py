# -*- coding: utf-8 -*-
"""v1.2.2 翻译功能单元验证：中文判定 / LLM 返回解析 / 译文优先级合并。"""
import sys, io
from pathlib import Path
# discover 模式下 test_link_spam.py 可能已把 stdout 包装为 UTF-8；
# 再包一层会让旧 wrapper 被 GC 并关闭底层 buffer，故只在必要时包装
if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from translator import is_chinese_text, _parse_translation_response
from database import _pick_translation

fails = 0
def check(desc, got, expect):
    global fails
    ok = got == expect
    if not ok:
        fails += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}: got={got!r} expect={expect!r}")

# ---- is_chinese_text ----
check("纯英文视为非中文", is_chinese_text("The battery life is amazing"), False)
check("纯中文视为中文", is_chinese_text("低温环境下续航衰减严重"), True)
check("中文夹杂英文品牌名", is_chinese_text("低温环境下 Unihertz Tank 续航衰减严重"), True)
check("英文夹杂个别中文字符仍算英文", is_chinese_text("This phone is 绝了 but battery dies fast in cold weather"), False)
check("纯表情/数字视为中文（无需翻译）", is_chinese_text("🔥🔥🔥 100%"), True)
check("空文本视为中文", is_chinese_text(""), True)

# ---- _parse_translation_response ----
ids = ["c1", "c2", "c3"]
check("标准JSON数组", _parse_translation_response(
    '[{"id":"c1","zh":"翻译一"},{"id":"c2","zh":"翻译二"}]', ids), {"c1": "翻译一", "c2": "翻译二"})
check("markdown包裹", _parse_translation_response(
    '```json\n[{"id":"c1","zh":"翻译一"}]\n```', ids), {"c1": "翻译一"})
check("前后噪声容错", _parse_translation_response(
    '以下是翻译结果：\n[{"id":"c3","zh":"翻译三"}]\n希望对你有帮助', ids), {"c3": "翻译三"})
check("忽略未知id", _parse_translation_response(
    '[{"id":"cX","zh":"垃圾"},{"id":"c2","zh":"翻译二"}]', ids), {"c2": "翻译二"})
check("忽略空译文", _parse_translation_response(
    '[{"id":"c1","zh":""},{"id":"c2","zh":"翻译二"}]', ids), {"c2": "翻译二"})
check("完全无法解析返回空", _parse_translation_response("抱歉我无法完成", ids), {})
check("非字符串输入返回空", _parse_translation_response(None, ids), {})

# ---- _pick_translation 优先级 ----
check("缓存优先", _pick_translation("缓存译文", "分析译文", "摘要"), ("缓存译文", "cache"))
check("无缓存用分析全文", _pick_translation("", "分析译文", "摘要"), ("分析译文", "analysis"))
check("仅摘要兜底", _pick_translation(None, None, "摘要"), ("摘要", "summary"))
check("全空", _pick_translation("", "", ""), ("", ""))
check("缓存为空白字符串不算", _pick_translation("   ", "分析译文", None), ("分析译文", "analysis"))

print()
print("ALL OK" if fails == 0 else f"{fails} FAILED")
if __name__ == "__main__":
    sys.exit(0 if fails == 0 else 1)
