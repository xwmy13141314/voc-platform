# VoC 痛点挖掘平台

> 自动化竞品社媒评论抓取 + AI 痛点结构化分析 + 产品改良建议生成

![Version](https://img.shields.io/badge/version-v0.5-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![Python](https://img.shields.io/badge/python-3.10+-yellow)
![LLM](https://img.shields.io/badge/LLM-5%20Providers-orange)

**VoC 痛点挖掘平台** 是一款面向三防手机（Rugged Phone）赛道的桌面应用，由 RugOne 团队开发。平台自动从海外社交媒体抓取竞品产品评论，借助多 LLM 完成结构化痛点提取，并通过可视化看板与 AI 报告输出可落地的产品改良建议。

目标竞品品牌：**Blackview、Ulefone、Doogee、Oukitel、Unihertz** 等三防手机厂商。

---

## 目录

- [系统流程](#-系统流程)
- [功能特性](#-功能特性)
- [界面截图](#-界面截图)
- [技术栈](#-技术栈)
- [快速开始](#-快速开始)
- [使用指南](#-使用指南)
- [LLM 配置](#-llm-配置)
- [项目结构](#-项目结构)
- [数据库设计](#-数据库设计)
- [API 文档](#-api-文档)
- [从源码构建](#-从源码构建)
- [平台支持](#-平台支持)
- [贡献指南](#-贡献指南)
- [开源协议](#-开源协议)

---

## 系统流程

```
┌─────────────────────────────────────────────────────────────────────┐
│                        VoC 痛点挖掘平台                              │
├──────────────┬──────────────────┬────────────────┬──────────────────┤
│  第一层       │  第二层           │  第三层         │  第四层           │
│  数据采集     │  AI 分析          │  存储与检索      │  洞察输出         │
├──────────────┼──────────────────┼────────────────┼──────────────────┤
│              │                  │                │                  │
│  YouTube     │  Gemini          │                │  ECharts 可视化   │
│  (yt-dlp)    │  DeepSeek        │  SQLite        │  - 标签分布       │
│              │                  │  (6 张表)       │  - 严重度饼图     │
│  Reddit      │  GLM (智谱)       │                │  - 优先级矩阵     │
│  (JSON API)  │                  │  brands        │  - 品牌热力图     │
│              │  Kimi (月之暗面)   │  products      │                  │
│  Instagram   │                  │  videos        │  AI 改良报告       │
│  (Instaloader)│  通义千问 (阿里)  │  comments      │  - 高频痛点改良   │
│              │                  │  analyses      │  - 竞品差距分析   │
│  TikTok      │  统一 OpenAI 接口  │  settings      │  - 微创新机会     │
│  (元数据)     │                  │                │  - 优先级 Top 10  │
│              │                  │                │                  │
└──────────────┴──────────────────┴────────────────┴──────────────────┘
     抓取                结构化分析          持久化存储         可视化与报告
```

---

## 功能特性

- **多平台评论抓取**：支持 YouTube、Reddit、Instagram 评论抓取，TikTok 支持视频元数据采集
- **多 LLM 痛点分析**：接入 5 家大模型提供商（Gemini / DeepSeek / GLM / Kimi / 通义千问），统一 OpenAI 兼容接口，一键切换
- **结构化痛点提取**：每条评论自动提取情感分、痛点类别、痛点标签、严重度、用户建议、匹配型号、中文摘要
- **品牌维度管理**：内置 5 大竞品品牌，支持增删改查，评论按品牌分组浏览
- **可视化洞察看板**：基于 ECharts 的 6 类图表（标签分布、严重度饼图、情感分布、优先级矩阵、品牌热力图、型号排名）
- **AI 改良报告**：LLM 自动生成包含高频痛点改良、竞品差距分析、微创新机会、优先级清单的产品建议报告
- **桌面应用体验**：pywebview 封装原生窗口，双击 exe 即用，无需安装 Python 环境
- **明暗主题切换**：支持浅色 / 深色主题，localStorage 持久化偏好
- **痛点表格交互**：表头筛选（型号、严重度、情感、标签）+ 列排序（严重度、情感、点赞数）
- **评论溯源链接**：分析详情支持跳转原始 YouTube 评论
- **批量轮询分析**：默认每批 500 条，多品牌轮询分配，确保各品牌公平覆盖
- **安全容错机制**：60 秒 LLM 请求超时 + 禁用自动重试 + 认证失败即时停止并提示

---

## 界面截图

> 以下为平台四个主标签页的界面示意，实际界面请运行应用后查看。

### 仪表盘

![仪表盘](./screenshots/dashboard.png)
*统计卡片 + 痛点标签排行 + 痛点列表（支持表头筛选与排序）*

### 数据采集

![数据采集](./screenshots/collection.png)
*平台选择 + 品牌选择 + 关键词输入 + 抓取进度 + 历史结果*

### 洞察看板

![洞察看板](./screenshots/insights.png)
*6 类 ECharts 可视化图表，多维交叉分析*

### 改良建议

![改良建议](./screenshots/suggestions.png)
*AI 生成的 Markdown 格式改良报告，4 大板块*

---

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| **后端框架** | FastAPI + Uvicorn | 高性能异步 Web 框架，提供全部 REST API |
| **前端界面** | 原生 HTML + ECharts + Marked.js | 单页应用，4 个标签页，ECharts 负责可视化，Marked.js 渲染 Markdown 报告 |
| **桌面封装** | pywebview | 将 Web UI 封装为原生桌面窗口，跨平台支持 |
| **数据库** | SQLite | 轻量级嵌入式数据库，6 张表，WAL 模式 |
| **视频抓取** | yt-dlp | YouTube / TikTok 视频搜索与评论提取 |
| **Reddit 抓取** | requests + Reddit JSON API | 无需认证，直接访问公共帖子与评论 |
| **Instagram 抓取** | Instaloader | 按 hashtag 搜索帖子并提取评论 |
| **AI 分析** | OpenAI SDK | 兼容多家 LLM 提供商，统一接口调用 |
| **语言检测** | langdetect | 自动识别评论语言 |
| **打包工具** | PyInstaller | 打包为单文件 `VoC-Platform.exe` |

---

## 快速开始

### 方式一：下载可执行文件（推荐非开发者使用）

1. 前往 [Releases 发布页](../../releases) 下载最新的 `VoC-Platform.exe`
2. 双击运行即可，无需安装 Python 或任何依赖
3. 首次启动后，进入设置页面配置 LLM 提供商和 API Key

> exe 运行时会在同级目录生成 `data/voc.db` 数据库文件。

### 方式二：从源码运行（开发者）

**环境要求**：Python 3.10+

```bash
# 1. 克隆项目
git clone <仓库地址>
cd voc-platform

# 2. 创建虚拟环境（推荐）
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量（可选，也可在应用内设置页面配置）
cp .env.example .env
# 编辑 .env 填入 GEMINI_API_KEY

# 5. 启动桌面应用
python app.py
```

> 也可仅启动后端服务（浏览器访问 `http://127.0.0.1:8000`）：
> ```bash
> python main.py
> ```

---

## 使用指南

启动应用后，顶部导航栏提供 4 个功能标签页：

### 1. 仪表盘

平台的核心总览页面，一屏掌握全局数据。

- **统计卡片**：总评论数、已分析数、品牌数、视频数、高严重度痛点数，卡片可点击跳转
- **痛点标签 Top 榜**：展示高频痛点标签排行
- **痛点列表表格**：
  - 表头筛选：按型号、严重度、情感、标签组合过滤
  - 列排序：支持按严重度、情感分、点赞数排序
  - 点击展开分析详情弹窗，查看完整结构化分析结果
  - 支持跳转原始 YouTube 评论链接

### 2. 数据采集

从海外社媒平台抓取竞品评论。

- **平台选择**：YouTube / Reddit / Instagram / TikTok（4 选 1）
- **品牌选择**：下拉菜单自动同步设置中的品牌列表
- **搜索关键词**：预填品牌默认关键词，可自定义修改
- **最大抓取数**：设置每个品牌抓取的视频/帖子数量上限
- **抓取按钮**：点击开始抓取，实时显示进度
- **结果与历史**：展示本次抓取结果（视频数、评论数、新增数）

### 3. 洞察看板

多维可视化分析，发现痛点规律。

| 图表 | 类型 | 说明 |
|------|------|------|
| 痛点标签分布 | 柱状图（Top 15） | 各痛点标签的提及频次，含严重度细分 |
| 严重度分布 | 饼图 | 轻微吐槽 / 影响体验 / 致命缺陷 占比 |
| 情感分布 | 饼图 | 极度负面 ~ 极度正面 5 级情感占比 |
| 优先级矩阵 | 散点图 | 痛点频率 × 平均严重度，四象限定位 |
| 品牌 × 痛点热力图 | 热力图 | 各品牌在各痛点维度的集中度 |
| 型号痛点排名 | 柱状图 | 按高严重度痛点数排序的产品型号 |

### 4. 改良建议

AI 自动生成产品改良报告。

点击「生成报告」按钮后，后端汇总痛点数据并调用 LLM 生成 Markdown 报告，包含 4 大板块：

- **一、高频痛点改良建议**：针对 Top 5 高频痛点，给出痛点描述、影响范围、改良方向、优先级（P0/P1/P2）
- **二、竞品差距分析**：各品牌在痛点维度的差异对比，找出需改进项与可借鉴项
- **三、微创新机会**：从用户评论中提炼可执行的微创新点，附用户原话依据和实现难度
- **四、改良优先级清单**：综合频率 × 严重度 × 用户需求，输出 Top 10 改良优先级

---

## LLM 配置

平台支持 5 家大模型提供商，全部通过 OpenAI 兼容接口接入，在应用内设置页面即可完成配置。

### 配置步骤

1. 打开应用，进入「设置」页面
2. 选择 LLM 提供商（5 选 1）
3. 填入对应提供商的 API Key
4. 选择模型（可选，留空则使用默认模型）
5. 点击「测试连接」验证配置是否有效
6. 保存配置

> 配置信息加密存储在 SQLite 数据库 `settings` 表中，API Key 在前端展示时自动脱敏。

### 支持的 LLM 提供商

| 提供商 | 默认模型 | 可选模型 | API Key 获取地址 |
|--------|----------|----------|-----------------|
| **Gemini** (Google) | `gemini-2.5-flash` | `gemini-2.5-flash`、`gemini-2.5-pro`、`gemini-1.5-flash` | https://aistudio.google.com/apikey |
| **DeepSeek** (深度求索) | `deepseek-chat` | `deepseek-chat`、`deepseek-reasoner` | https://platform.deepseek.com/api_keys |
| **GLM** (智谱清言) | `glm-4-flash` | `glm-4-flash`、`glm-4`、`glm-4-air` | https://open.bigmodel.cn/usercenter/apikeys |
| **Kimi** (月之暗面) | `moonshot-v1-8k` | `moonshot-v1-8k`、`moonshot-v1-32k`、`moonshot-v1-128k` | https://platform.moonshot.cn/console/api-keys |
| **通义千问** (阿里) | `qwen-turbo` | `qwen-turbo`、`qwen-plus`、`qwen-max` | https://dashscope.console.aliyun.com/apiKey |

> **提示**：推荐使用 Gemini（免费额度较多）或 DeepSeek（性价比高）。Kimi 的 128k 模型适合长文本分析场景。

---

## 项目结构

```
voc-platform/
├── app.py                  # 桌面应用入口（pywebview 窗口 + 内嵌 FastAPI 服务）
├── main.py                 # FastAPI 主应用，定义全部 API 端点
├── config.py               # 全局配置管理（数据库路径、API Key、抓取参数等）
├── database.py             # SQLite 数据库层（建表、增删改查、洞察聚合查询）
├── crawler.py              # 多平台评论抓取模块（YouTube / Reddit / Instagram / TikTok）
├── analyzer.py             # LLM 痛点分析模块（Prompt 构建 + 批量分析 + 结果解析）
├── llm_provider.py         # 多 LLM 提供商抽象层（统一 OpenAI 兼容接口）
├── static/
│   └── index.html          # 前端单页应用（4 个标签页 + ECharts + Marked.js）
├── data/
│   └── voc.db              # SQLite 数据库文件（运行时自动生成）
├── dist/
│   └── VoC-Platform.exe    # 打包后的可执行文件
├── voc-platform.spec       # PyInstaller 打包配置文件
├── requirements.txt        # Python 依赖清单
├── .env.example            # 环境变量配置模板
├── app_icon.ico            # 应用图标
├── LICENSE                 # MIT 开源协议
└── README.md               # 项目说明文档（本文件）
```

---

## 数据库设计

数据库使用 SQLite，文件路径为 `data/voc.db`，共 6 张表：

### 1. brands — 品牌表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT (PK) | 品牌 ID（UUID） |
| `name` | TEXT (UNIQUE) | 品牌名称 |
| `search_keyword` | TEXT | 默认搜索关键词 |
| `created_at` | TEXT | 创建时间 |

### 2. products — 产品表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT (PK) | 产品 ID（UUID） |
| `brand_id` | TEXT (FK) | 关联品牌 ID |
| `model` | TEXT | 产品型号 |
| `aliases` | TEXT | 型号别名（JSON） |
| `created_at` | TEXT | 创建时间 |

### 3. videos — 视频/帖子表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT (PK) | 记录 ID（UUID） |
| `video_id` | TEXT (UNIQUE) | 平台视频/帖子 ID |
| `title` | TEXT | 标题 |
| `channel` | TEXT | 频道/作者 |
| `view_count` | INTEGER | 播放/浏览量 |
| `comment_count` | INTEGER | 评论数 |
| `published_at` | TEXT | 发布时间 |
| `crawled_at` | TEXT | 抓取时间 |
| `brand_id` | TEXT (FK) | 关联品牌 ID |

### 4. comments — 评论表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT (PK) | 评论 ID（UUID） |
| `platform` | TEXT | 来源平台（youtube/reddit/instagram/tiktok） |
| `original_id` | TEXT | 平台原始评论 ID |
| `video_id` | TEXT (FK) | 关联视频/帖子 ID |
| `brand_id` | TEXT (FK) | 关联品牌 ID |
| `content` | TEXT | 原始评论内容 |
| `content_clean` | TEXT | 清洗后内容 |
| `language` | TEXT | 语言代码（如 en、zh） |
| `author` | TEXT | 评论作者 |
| `like_count` | INTEGER | 点赞数 |
| `posted_at` | TEXT | 评论发布时间 |
| `crawled_at` | TEXT | 抓取时间 |
| `sentiment_pre` | INTEGER | 预判情感分 |
| `analyzed` | INTEGER | 是否已分析（0=未分析，1=已分析） |

### 5. analyses — 分析结果表

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | TEXT (PK) | 分析 ID（UUID） |
| `comment_id` | TEXT (FK, UNIQUE) | 关联评论 ID |
| `sentiment_score` | INTEGER | 情感分（1=极度负面 ~ 5=极度正面） |
| `pain_categories` | TEXT | 痛点类别（JSON 数组：hardware/software/scenario/ecosystem） |
| `pain_tags` | TEXT | 痛点标签（JSON 数组：battery/screen/waterproof 等） |
| `severity` | INTEGER | 严重度（1=轻微吐槽，2=影响体验，3=致命缺陷） |
| `user_solution` | TEXT | 用户提出的改良建议 |
| `product_match` | TEXT | 匹配的产品型号 |
| `summary_zh` | TEXT | 50 字中文摘要 |
| `llm_model` | TEXT | 使用的 LLM 模型 |
| `analyzed_at` | TEXT | 分析时间 |
| `human_corrected` | INTEGER | 是否人工修正（0=否，1=是） |

### 6. settings — 配置表

| 字段 | 类型 | 说明 |
|------|------|------|
| `key` | TEXT (PK) | 配置键名 |
| `value` | TEXT | 配置值 |
| `updated_at` | TEXT | 更新时间 |

> settings 表存储 LLM 提供商选择、各提供商的 API Key、模型选择等配置。

---

## API 文档

应用后端运行在 `http://127.0.0.1:8000`，提供以下 REST API：

### 页面

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 主页面（返回 index.html） |

### 数据统计与查询

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET | `/api/stats` | — | 获取全局统计数据（评论数、分析数、品牌数等） |
| GET | `/api/pain-points` | `brand`, `platform`, `min_severity`, `limit` | 获取痛点列表（已分析评论 + 分析结果） |
| GET | `/api/comments` | `analyzed`, `brand`, `limit`, `offset` | 获取评论列表（支持分页） |
| GET | `/api/comments/grouped` | — | 获取按品牌分组的评论统计 + 每组前 5 条 |
| GET | `/api/analyses` | `brand`, `min_severity`, `limit`, `offset` | 获取分析结果详情（含评论原文） |

### LLM 配置

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET | `/api/llm/providers` | — | 获取所有支持的 LLM 提供商列表 |
| GET | `/api/llm/config` | — | 获取当前 LLM 配置（API Key 脱敏） |
| POST | `/api/llm/config` | Body: `LLMConfigModel` | 保存 LLM 配置 |
| POST | `/api/llm/test` | Body: `TestConnectionModel` | 测试 LLM 连接是否可用 |

### 品牌管理

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET | `/api/brands` | — | 获取所有品牌（含评论数、分析数统计） |
| POST | `/api/brands` | Body: `BrandModel` | 添加品牌 |
| PUT | `/api/brands/{id}` | Body: `BrandModel` | 修改品牌信息 |
| DELETE | `/api/brands/{id}` | — | 删除品牌及其关联数据 |

### 数据抓取与分析

| 方法 | 路径 | 参数 | 说明 |
|------|------|------|------|
| GET | `/api/platforms` | — | 获取支持的平台列表及能力说明 |
| POST | `/api/crawl` | `brand_name`, `search_keyword`, `max_videos`, `platform` | 执行评论抓取 |
| POST | `/api/analyze` | `limit`, `brand` | 执行批量 AI 痛点分析 |

### 洞察可视化

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/insights/tag-distribution` | 痛点标签分布（含严重度细分） |
| GET | `/api/insights/brand-tag-matrix` | 品牌 × 痛点交叉矩阵（热力图数据） |
| GET | `/api/insights/model-ranking` | 型号痛点排名 |
| GET | `/api/insights/priority-matrix` | 优先级矩阵（频率 × 严重度） |
| GET | `/api/insights/severity-distribution` | 严重度分布（饼图数据） |
| GET | `/api/insights/sentiment-distribution` | 情感分布（饼图数据） |
| GET | `/api/insights/summary` | 痛点数据汇总（报告生成数据源） |

### AI 报告

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/insights/generate-report` | 调用 LLM 生成产品改良建议报告 |

---

## 从源码构建

如需自行打包为 exe 可执行文件，使用 PyInstaller 进行构建：

### 1. 安装打包依赖

```bash
pip install pyinstaller>=6.3.0
```

### 2. 执行打包

```bash
# 使用项目自带的 spec 配置文件打包
pyinstaller voc-platform.spec
```

### 3. 获取产物

打包完成后，可执行文件位于：

```
dist/VoC-Platform.exe
```

### 打包说明

`voc-platform.spec` 配置要点：

- 入口文件：`app.py`
- 打包含：FastAPI 后端 + pywebview 窗口 + yt-dlp + Instaloader + 多 LLM SDK
- 静态资源：`static/` 目录随 exe 打包
- 图标：`app_icon.ico`
- 排除项：matplotlib、numpy、pandas、tkinter 等不必要的大型库
- UPX 压缩：启用，减小体积

> 运行 exe 时会在同级目录自动创建 `data/` 文件夹存放数据库。

---

## 平台支持

| 平台 | 搜索 | 评论抓取 | 抓取引擎 | 说明 |
|------|:----:|:--------:|----------|------|
| **YouTube** | 支持 | 支持 | yt-dlp | 搜索视频并提取评论，支持高赞排序 |
| **Reddit** | 支持 | 支持 | Reddit JSON API | 无需认证，搜索帖子并提取评论 |
| **Instagram** | 支持 | 支持 | Instaloader | 按 hashtag 搜索帖子并提取评论 |
| **TikTok** | 支持 | 不支持 | yt-dlp | 仅抓取视频元数据，评论暂不支持 |

### 痛点分析维度

AI 分析时会从每条评论中提取以下结构化信息：

**痛点类别（pain_categories）**：

| 类别 | 说明 |
|------|------|
| `hardware` | 硬件问题 |
| `software` | 软件 / 系统问题 |
| `scenario` | 特定场景失效 |
| `ecosystem` | 配件 / 生态问题 |

**痛点标签（pain_tags）**：

`battery`（电池）、`screen`（屏幕）、`waterproof`（防水）、`system`（系统）、`weight`（重量）、`signal`（信号）、`camera`（摄像头）、`button`（按键）、`charging`（充电）、`durability`（耐用性）、`app_pairing`（配对）、`ota`（升级）、`ui`（界面）、`delay`（延迟）

**严重度（severity）**：

| 分值 | 含义 |
|------|------|
| 1 | 轻微吐槽 |
| 2 | 影响体验 |
| 3 | 致命缺陷 |

**情感分（sentiment_score）**：

| 分值 | 含义 |
|------|------|
| 1 | 极度负面 |
| 2 | 负面 |
| 3 | 中性 |
| 4 | 正面 |
| 5 | 极度正面 |

### 默认竞品品牌

应用首次启动时自动初始化以下竞品品牌：

| 品牌 | 默认搜索关键词 |
|------|---------------|
| Blackview | Blackview rugged phone review |
| Ulefone | Ulefone Armor review |
| Doogee | Doogee rugged phone review |
| Oukitel | Oukitel rugged phone review |
| Unihertz | Unihertz rugged phone review |

---

## 贡献指南

欢迎为本项目贡献代码！请遵循以下流程：

1. **Fork** 本仓库
2. 创建功能分支：`git checkout -b feature/your-feature`
3. 提交更改：`git commit -m 'feat: 添加某功能'`
4. 推送分支：`git push origin feature/your-feature`
5. 提交 **Pull Request**

### 开发约定

- 提交信息遵循 Conventional Commits 规范（`feat:` / `fix:` / `docs:` / `refactor:`）
- 新增功能请确保不破坏现有 API 兼容性
- 数据库结构变更需提供迁移脚本
- 代码风格保持与现有模块一致

### 本地开发

```bash
# 克隆并安装依赖
git clone <仓库地址>
cd voc-platform
pip install -r requirements.txt

# 开发模式运行（修改代码后重启即生效）
python main.py
# 浏览器访问 http://127.0.0.1:8000
```

---

## 开源协议

本项目基于 [MIT License](./LICENSE) 开源。

Copyright (c) 2026 RugOne Team

---

<p align="center">
  VoC 痛点挖掘平台 · RugOne Team · 2026
</p>
