"""sources 包：平台抓取器的模块化实现。

v2.0 起爬虫从 crawler.py 单文件逐步拆分到本包：
- common.py        共享文本工具（清洗 / 语言检测）
- quality_filter.py 评论质量过滤引擎（W1-6）
- aliexpress.py    AliExpress 公开评论 API（W1-2）

crawler.py 通过 PLATFORM_REGISTRY 引用本包模块；本包不得反向 import crawler，
避免循环依赖。存储统一走 crawler._store_comments / database。
"""
