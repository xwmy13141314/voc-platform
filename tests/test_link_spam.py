# -*- coding: utf-8 -*-
"""link_spam 规则单元验证：拦广告（短残留+促销话术）、不误伤真实评论。"""
import sys, io
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.common import clean_comment
from sources.quality_filter import evaluate_comment, DEFAULT_CONFIG

cases = [
    # ---- 应过滤：短残留广告 ----
    ("广告-OUKITEL导购", "🛒 OUKITEL WP66 BUY HERE ➜ https://oukitel.store/products/oukitel-wp66-5g-rugged-phone", False),
    ("广告-速卖通联盟", "Get yours here - https://s.click.aliexpress.com/e/_okokAIF", False),
    ("广告-HOTWAV短链", "👉 Buy HOTWAV W11 at Official store: https://bit.ly/3Pymeso", False),
    ("广告-纯链接眨眼", "https://www.thuiswinkel.org/leden/belsimpel.nl/certificaat ;)", False),
    # ---- 应过滤：促销话术广告（残留较长）----
    ("广告-优惠码", "Grab Your WP60 Here: USE CODE OUK10 for 10% OFF! https://oukitel.com/products/wp60", False),
    ("广告-Ulefone最佳价", "Get The Ulefone Armor 28 Ultra Here: 🔥 https://bit.ly/3DQtMnJ", False),
    ("广告-双平台购买", "👉 Buy on Aliexpress: https://s.click.aliexpress.com/e/_oEKnkj2 👉 Buy on Shopify: https://bit.ly/49iPPgs", False),
    ("广告-专属折扣", "Special Discount For My Viewers! 🛒 Get the Blackview BL7000 here: http://bit.ly/45DE6tg 💰 Discount Code: APH7Q", False),
    ("广告-榜单链接", "🎯 Links to the best 5 blackview rugged smartphones we listed in this video: 1. Blackview Xplore 2- https://bit.ly/xyz 2. Blackview A5- https://bit.ly/abc", False),
    ("广告-VPN赞助", "If you plan on going offgrid - take Surfshark with you! Go to https://surfshark.com/jerryrig or use code JERRYRIG", False),
    ("广告-水壶赞助", "Aluminum Jerrycans are just 3 bucks for a 12 pack! Use code: THIRSTY at https://www.DrinkBison.com (If you're thirsty)", False),
    # ---- 应通过：真实用户评论 ----
    ("正常-带链接长评论", "I bought this phone last month from the official store here https://example.com/shop and the battery life has been amazing for my construction job", False),
    ("正常-无链接", "That drop test was savage, still intact? Or it broke after?", True),
    ("正常-中文无链接", "低温环境下续航衰减严重，电池不耐用", True),
    ("正常-链接+中长残留", "Here is the detailed teardown video you asked about https://youtube.com/watch?v=xxx it shows the battery placement clearly", True),
    ("正常-真实用户提视频", "I think it did break the top of my foot LOL. I did a follow up in the video for the Tank 3S here https://youtu.be/xxx", True),
    ("正常-真实推荐语", "They are fun. The newer 4 Pro and Tank X have some solid performance and projector upgrades too. https://www.youtube.com/watch?v=yyy", True),
    ("正常-荷兰语答疑", "Hi Nourdin, Je kunt bij de instellingen van je toetsenbord komen door de komma-toets lang ingedrukt te houden https://support.example.com/kb", True),
]

cfg = dict(DEFAULT_CONFIG)
fails = 0
for desc, raw, expect_pass in cases:
    cleaned = clean_comment(raw)
    v = evaluate_comment(cleaned, "youtube", 3, 0, cfg=cfg, raw_content=raw)
    ok = v.passed == expect_pass
    if not ok:
        fails += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {desc}: passed={v.passed} reason={v.reason or '-'}")
print()
print("ALL OK" if fails == 0 else f"{fails} FAILED")
