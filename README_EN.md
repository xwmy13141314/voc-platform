# VoC Pain Point Mining Platform

> 中文文档 | [English Documentation](README_EN.md) | [中文文档 (Chinese)](README.md)

![Version](https://img.shields.io/badge/version-v1.2.0-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![Python](https://img.shields.io/badge/python-3.12%20x64-yellow)
![Status](https://img.shields.io/badge/status-active-success)

A desktop application for automated competitor social media analysis and product pain point mining, built for the **RugOne** team. The platform automatically crawls competitor product reviews from overseas social media platforms, uses AI (LLM) for structured pain point analysis, and generates actionable product improvement guidance for the rugged phone market.

**Target Brands:** Blackview, Ulefone, Doogee, Oukitel, Unihertz, and more.

> **v1.2.0 (2026-09-01) — v2.0 upgrade Phase 1+2:** adds a configurable comment quality filter engine (incl. link-spam / promo-comment blocking), AliExpress e-commerce review ingestion, global pain point clustering (BGE-M3 multilingual embeddings + UMAP/HDBSCAN, 79 topic clusters with Chinese naming), and a dedicated Clustering page; GLM adds glm-5.2/5.3; detailed LLM 429 diagnostics. Distribution directory: `release-v1.2/`. Full history: [CHANGELOG.md](./CHANGELOG.md).

---

## 🔄 System Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          DATA CRAWLING LAYER                            │
│                                                                         │
│   YouTube (yt-dlp)  Reddit (JSON+RSS)    Instagram   TikTok (metadata) │
│         │                  │            (Instaloader)      │             │
│         └──────────┬───────┴───────────────┴──────────────┘             │
│                    ▼                                                     │
├─────────────────────────────────────────────────────────────────────────┤
│                     STORAGE & RETRIEVAL LAYER                           │
│                                                                         │
│                     SQLite Database (7 tables)                          │
│   brands · products · videos · comments · analyses · settings · jobs    │
│                              │                                          │
├──────────────────────────────┼──────────────────────────────────────────┤
│                    AI ANALYSIS LAYER                                    │
│                              ▼                                          │
│        Gemini · DeepSeek · GLM · Kimi · Qwen (OpenAI-compatible)        │
│                                                                         │
│   Structured pain point extraction: sentiment · categories · severity   │
│                              │                                          │
├──────────────────────────────┼──────────────────────────────────────────┤
│                      INSIGHT OUTPUT LAYER                               │
│                              ▼                                          │
│          ECharts Visualizations + AI Improvement Reports                │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## ✨ Features

- **Multi-Platform Collection** — Best-effort public collection from YouTube, Reddit, and Instagram; TikTok metadata support. Availability depends on network region, login state, and platform anti-bot changes
- **AI-Powered Pain Point Analysis** — Structured extraction of sentiment, pain categories, severity levels, and user-suggested solutions via LLM
- **Multi-LLM Support** — Compatible with 5 leading LLM providers (Gemini, DeepSeek, GLM, Kimi, Qwen) through a unified OpenAI-compatible interface
- **Interactive Dashboard** — Stats cards, top pain tags, and a filterable/sortable pain points table
- **ECharts Visualizations** — Pain tag distribution, severity/sentiment pie charts, priority matrix scatter plot, brand×tag heatmap, and model pain ranking
- **AI-Generated Improvement Reports** — Automated 4-section reports covering high-frequency pain improvements, competitor gap analysis, micro-innovation opportunities, and a prioritized improvement list
- **Light/Dark Theme** — Theme toggle with localStorage persistence
- **Brand-Grouped Comment Browser** — Clickable stats cards that drill into brand-specific comments
- **Source Traceability** — Public source URLs for videos, posts, and comments
- **Truthful Job Outcomes** — Distinguishes success, no matching results, failure, and cancellation; network/parser errors are never reported as success
- **Batch Analysis** — Configurable batch size (default 500) with round-robin across brands for balanced coverage
- **Standalone Executable** — Package as a single `VoC-Platform.exe` — no Python installation required
- **Robust Error Handling** — 60-second LLM timeout with disabled auto-retry and clear authentication error messages

---

## 📸 Screenshots

### Dashboard
![Dashboard](docs/screenshots/dashboard.png)
*Overview of stats cards, top pain tags, and the filterable pain points table.*

### Data Collection
![Data Collection](docs/screenshots/data-collection.png)
*Select platform, brand, and keywords to start crawling competitor reviews.*

### Insights Dashboard
![Insights Dashboard](docs/screenshots/insights.png)
*ECharts visualizations including tag distribution, severity pie, priority matrix, and heatmap.*

### Improvement Suggestions
![Improvement Suggestions](docs/screenshots/improvement-report.png)
*AI-generated product improvement report with prioritized action items.*

---

## 🛠️ Tech Stack

| Category | Technology | Description |
|----------|-----------|-------------|
| Backend | FastAPI + Uvicorn | High-performance async web framework |
| Frontend | HTML + ECharts + Marked.js | Single-page UI with rich data visualization |
| Desktop | pywebview | Wraps web UI in a native desktop window |
| Database | SQLite | Lightweight embedded database (7 tables, including jobs) |
| Crawling | yt-dlp, Instaloader, requests | Multi-platform data collection |
| AI | OpenAI SDK | Compatible with multiple LLM providers |
| Language Detection | langdetect | Automatic comment language detection |
| Packaging | PyInstaller | Builds standalone Windows executable |

---

## 🚀 Quick Start

### Method A: Download the Executable (For Non-Developers)

1. Obtain the complete `release-v1.2/` directory; do not mix it with old `dist-*` or historical release folders
2. Double-click `release-v1.2/VoC-Platform.exe` — no Python installation needed
3. Configure your LLM API key in the Settings page (see [LLM Configuration](#-llm-configuration))

Packaged data is stored at `%LOCALAPPDATA%\VoC-Platform\data\voc.db`. On first launch only, the bundled `release-v1.2/data/voc.db` seed is copied when no local database exists (an empty schema-only database is also treated as "no data" and gets seeded); existing local data is never overwritten.

> Desktop-lite build note: the clustering science stack (torch / sentence-transformers / umap / hdbscan, ~1GB+) is intentionally excluded from the EXE. Viewing clusters, drill-down and AI renaming work in the desktop app; re-running clustering requires source mode.

### Method B: Run from Source (For Developers)

**Prerequisites:** Windows x64 and CPython 3.12 x64

```bash
# 1. Clone the repository
git clone https://github.com/RugOne/voc-platform.git
cd voc-platform

# 2. (Optional) Create and activate a virtual environment
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
# source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# Edit .env and add your Gemini API key

# 5. Launch the application
python app.py
```

The desktop application selects a private random loopback port and verifies the v0.8 instance token before opening its window, so it cannot silently attach to an older service on port 8000. Running `main.py` directly still defaults to `http://127.0.0.1:8000` for development.

---

## 📖 Usage Guide

The application features a single-page interface with 4 navigation tabs:

### 1. Dashboard (仪表盘)

The main overview panel displaying:
- **Stats Cards** — Total comments, analyzed comments, brands, videos, and high-severity pain points
- **Top Pain Tags** — Most frequently mentioned pain point categories
- **Pain Points Table** — A comprehensive table with header filtering (model, severity, sentiment, tags) and column sorting (severity, sentiment, like count). Click any row to view the full analysis detail in a modal.

### 2. Data Collection (数据采集)

The crawling control panel where you:
- **Select a platform** — YouTube, Reddit, Instagram, or TikTok
- **Select a brand** — Auto-syncs from your settings; pre-loaded with 5 default competitors
- **Enter search keywords** — Customize the search terms for review discovery
- **Set max crawl count** — Limit the number of comments to collect
- **Start crawling** — Click the crawl button and monitor real-time progress
- **View results/history** — Browse previously crawled data

### 3. Insights Dashboard (洞察看板)

Interactive ECharts visualizations for data-driven decision making:
- **Pain Tag Distribution** — Top 15 pain tags as a horizontal bar chart
- **Severity Distribution** — Pie chart showing the breakdown of pain point severity levels
- **Sentiment Distribution** — Pie chart showing positive/neutral/negative sentiment ratios
- **Priority Matrix** — Frequency × severity scatter plot to identify high-priority issues
- **Brand × Pain Tag Heatmap** — Matrix showing which brands struggle with which pain points
- **Model Pain Ranking** — Bar chart ranking product models by pain point frequency

### 4. Improvement Suggestions (改良建议)

An AI-generated report with 4 sections:
1. **High-Frequency Pain Improvements** — Address the most commonly reported issues
2. **Competitor Gap Analysis** — Identify where competitors fall short
3. **Micro-Innovation Opportunities** — Small changes that create differentiation
4. **Improvement Priority List** — Top 10 prioritized action items

---

## 🤖 LLM Configuration

The platform supports 5 LLM providers, all accessible through an OpenAI-compatible API interface. Configure your preferred provider in the **Settings** page within the application:

1. Open the application
2. Navigate to **Settings**
3. Select your preferred LLM provider
4. Enter your API key
5. Select a model
6. Click **Test** to verify the connection
7. Save the configuration

### Supported Providers

| Provider | Models | Get API Key |
|----------|--------|-------------|
| **Gemini** (Google) | `gemini-2.5-flash`, `gemini-2.5-pro` | [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| **DeepSeek** | `deepseek-chat`, `deepseek-reasoner` | [https://platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) |
| **GLM** (Zhipu) | `glm-4-flash`, `glm-4`, `glm-4-air` | [https://open.bigmodel.cn/usercenter/apikeys](https://open.bigmodel.cn/usercenter/apikeys) |
| **Kimi** (Moonshot) | `moonshot-v1-8k`, `moonshot-v1-32k`, `moonshot-v1-128k` | [https://platform.moonshot.cn/console/api-keys](https://platform.moonshot.cn/console/api-keys) |
| **Qwen** (Alibaba) | `qwen-turbo`, `qwen-plus`, `qwen-max` | [https://dashscope.console.aliyun.com/apiKey](https://dashscope.console.aliyun.com/apiKey) |

> **Note:** The LLM request timeout is set to 60 seconds with auto-retry disabled. If you encounter authentication errors, verify your API key is correct and has not expired.

### Environment Variable Configuration

For the default Gemini provider, you can also configure via environment variables:

```bash
# Copy the template
cp .env.example .env

# Edit .env and add your key
GEMINI_API_KEY=your_api_key_here
```

---

## 📁 Project Structure

```
voc-platform/
├── app.py                  # Desktop app entry point (pywebview + FastAPI)
├── version.py              # Shared app/schema version identity
├── main.py                 # FastAPI application with all API endpoints
├── config.py               # Configuration management
├── database.py             # SQLite database layer
├── crawler.py              # Multi-platform comment crawling module
├── analyzer.py             # LLM-based pain point analysis module
├── llm_provider.py         # Multi-LLM provider abstraction layer
├── requirements.txt        # Python dependencies
├── voc-platform.spec       # PyInstaller spec file
├── .env.example            # Environment config template
├── app_icon.ico            # Application icon
├── tests/                  # test_smoke.py + test_link_spam.py regression checks
├── release-v1.2/           # Canonical packaged release (EXE + database seed)
├── LICENSE                 # MIT License
├── static/
│   └── index.html          # Frontend UI (single page, 4 tabs)
└── data/
    └── voc.db              # SQLite database (auto-generated)
```

---

## 🗄️ Database Schema

The application uses SQLite with 7 tables. Source runs use `data/voc.db`; packaged runs use `%LOCALAPPDATA%\VoC-Platform\data\voc.db` by default:

### 1. `brands`
Stores competitor brand information.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER (PK) | Primary key |
| `name` | TEXT | Brand name |
| `search_keyword` | TEXT | Default search keyword for crawling |
| `brand_type` | TEXT | `competitor`, `own`, or benchmark type |
| `created_at` | TIMESTAMP | Record creation time |

### 2. `products`
Stores product models associated with each brand.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER (PK) | Primary key |
| `brand_id` | INTEGER (FK) | Reference to `brands.id` |
| `model` | TEXT | Product model name |
| `aliases` | TEXT | Alternative names / aliases |
| `created_at` | TIMESTAMP | Record creation time |

### 3. `videos`
Stores crawled video/post metadata.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER (PK) | Primary key |
| `video_id` | TEXT | Platform-specific video/post ID |
| `platform` | TEXT | Source platform |
| `external_id` | TEXT | Original platform ID |
| `title` | TEXT | Video/post title |
| `channel` | TEXT | Channel or author name |
| `view_count` | INTEGER | View count |
| `comment_count` | INTEGER | Comment count |
| `published_at` | TIMESTAMP | Original publish date |
| `crawled_at` | TIMESTAMP | Crawl timestamp |
| `brand_id` | INTEGER (FK) | Reference to `brands.id` |
| `source_url` | TEXT | Public source URL |

### 4. `comments`
Stores individual crawled comments.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER (PK) | Primary key |
| `platform` | TEXT | Source platform (YouTube/Reddit/Instagram/TikTok) |
| `original_id` | TEXT | Original comment ID on the platform |
| `video_id` | TEXT | Associated video/post ID |
| `brand_id` | INTEGER (FK) | Reference to `brands.id` |
| `content` | TEXT | Original comment content |
| `content_clean` | TEXT | Cleaned/preprocessed content |
| `language` | TEXT | Detected language |
| `author` | TEXT | Comment author |
| `like_count` | INTEGER | Like count |
| `posted_at` | TIMESTAMP | Original comment timestamp |
| `source_url` | TEXT | Comment or post source URL |
| `crawled_at` | TIMESTAMP | Crawl timestamp |
| `sentiment_pre` | TEXT | Pre-analysis sentiment guess |
| `analyzed` | BOOLEAN | Whether AI analysis has been performed |

### 5. `analyses`
Stores AI analysis results for each comment.

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER (PK) | Primary key |
| `comment_id` | INTEGER (FK) | Reference to `comments.id` |
| `sentiment_score` | REAL | AI sentiment score |
| `pain_categories` | TEXT (JSON) | Pain point categories |
| `pain_tags` | TEXT (JSON) | Specific pain point tags |
| `severity` | TEXT | Severity level (low/medium/high) |
| `user_solution` | TEXT | User-suggested solution (if any) |
| `product_match` | TEXT | Matched product model |
| `summary_zh` | TEXT | Chinese summary of the analysis |
| `translation_zh` | TEXT | Chinese translation of the comment |
| `confidence` | REAL | AI confidence from 0 to 1 |
| `llm_model` | TEXT | LLM model used for analysis |
| `prompt_version` | TEXT | Prompt version |
| `analyzed_at` | TIMESTAMP | Analysis timestamp |
| `human_corrected` | BOOLEAN | Whether a human has corrected the result |

### 6. `settings`
Stores application configuration as key-value pairs.

| Column | Type | Description |
|--------|------|-------------|
| `key` | TEXT (PK) | Setting key |
| `value` | TEXT | Setting value |
| `updated_at` | TIMESTAMP | Last update time |

### 7. `jobs`
Stores asynchronous crawl/analysis task parameters, status, progress, errors, and local JSON results. Crawl outcomes include `succeeded`, `empty`, `failed`, and `cancelled`.

All source data and task history remain local by default. On Windows, API keys use DPAPI encryption when available; legacy plaintext values remain readable for migration.

---

## 🔌 API Documentation

The embedded FastAPI server exposes the following endpoints:

### Pages

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Main page (serves the frontend UI) |

### Statistics

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/stats` | Dashboard statistics (total comments, analyzed, brands, videos, high severity) |

### Pain Points

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/pain-points` | Pain point list (filters: `brand`, `platform`, `min_severity`, `limit`) |

### Comments

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/comments` | Comment list (filters: `analyzed`, `brand`, `limit`, `offset`) |
| GET | `/api/comments/grouped` | Comments grouped by brand |

### Analyses

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/analyses` | Analysis results (filters: `brand`, `min_severity`, `limit`, `offset`) |
| GET | `/api/analyses/{comment_id}` | Get one analysis and correction status |
| PUT | `/api/analyses/{comment_id}` | Save a human-corrected analysis |

### LLM Configuration

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/llm/providers` | List all supported LLM providers and models |
| GET | `/api/llm/config` | Get current LLM configuration |
| POST | `/api/llm/config` | Update LLM configuration |
| POST | `/api/llm/test` | Test LLM connection with current config |

### Brands

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/brands` | List all brands |
| POST | `/api/brands` | Create a new brand |
| PUT | `/api/brands/{id}` | Update a brand |
| DELETE | `/api/brands/{id}` | Delete a brand |

### Crawling

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/crawl` | Start a crawling task |
| GET | `/api/platforms` | List supported platforms |
| GET | `/api/jobs` | List local task history |
| GET | `/api/jobs/{job_id}` | Poll task status, progress, and result |
| POST | `/api/jobs/{job_id}/cancel` | Request cancellation of a queued/running task |

### Analysis

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/analyze` | Run AI analysis (params: `limit`, `brand`) |
| GET | `/api/health` | Local service and database health check |

### Insights

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/insights/tag-distribution` | Pain tag distribution data |
| GET | `/api/insights/brand-tag-matrix` | Brand × pain tag heatmap matrix |
| GET | `/api/insights/model-ranking` | Model pain ranking data |
| GET | `/api/insights/priority-matrix` | Priority matrix (frequency × severity) |
| GET | `/api/insights/severity-distribution` | Severity distribution data |
| GET | `/api/insights/sentiment-distribution` | Sentiment distribution data |
| GET | `/api/insights/summary` | Overall insights summary |

### Reports

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/insights/generate-report` | Generate AI improvement report |

---

## 📦 Build from Source

To package the application as a standalone Windows executable using PyInstaller:

**Build environment:** CPython 3.12 x64 and PyInstaller 6.21.0

```bash
# 1. Install locked dependencies
pip install -r requirements.txt

# 2. Build in versioned directories (no --clean; it also wipes the shared PyInstaller cache)
python -m PyInstaller --noconfirm --distpath .\dist-v1.2 --workpath .\build-v1.2 .\voc-platform.spec

# 3. The executable will be generated in:
#    dist-v1.2/VoC-Platform.exe

# 4. Assemble the release folder: copy the EXE plus the current data/voc.db
#    seed into release-v1.2/, then verify with:
#    release-v1.2\VoC-Platform.exe --self-test
```

The `voc-platform.spec` file contains all build configuration including:
- Entry point (`app.py`)
- Bundled data files (`static/`, `app_icon.ico`)
- Hidden imports for pywebview, yt-dlp, `jiter`, and `pydantic_core`
- Excludes for the clustering science stack (torch, sentence-transformers, umap, hdbscan — desktop-lite build keeps the EXE ~44MB)
- Window settings and application icon

> **Note:** The built executable includes all dependencies. End users do not need Python installed to run `VoC-Platform.exe`.

---

## 🌐 Platform Support

| Platform | Crawling Method | Comments | Metadata | Notes |
|----------|----------------|----------|----------|-------|
| **YouTube** | yt-dlp | Conditional | Conditional | Best-effort public extraction; may require cookies or suitable regional access |
| **Reddit** | JSON API + RSS fallback | Conditional | Conditional | Anonymous endpoints may return 403; failures are surfaced explicitly |
| **Instagram** | Instaloader | Conditional | Conditional | Some content requires login and may trigger 401/429 |
| **TikTok** | Metadata only | Not supported | Supported | Comment crawling not supported; metadata only |

### v0.8 Release Verification

- 7 automated smoke tests passed
- Packaged `jiter.cp312`, `pydantic_core.cp312`, static assets, and SQLite self-test passed
- Seed data: 6,390 comments, 588 analyses, 12 brands, and 83 videos/posts
- EXE SHA-256: `6012A3ADC1CFFECB09D444EF6B96014E8FFA117018A901EAC83FEB0C17CA7D49`

---

## 🎯 Default Competitor Brands

The platform comes pre-loaded with 8 competitor brands and RugOne as the own-brand benchmark. Crawling and analysis run as asynchronous local jobs; poll `/api/jobs/{job_id}` for progress. Job history and all source data are kept in the local SQLite database. On Windows, API keys are stored with DPAPI when available.

| Brand | Default Search Keyword |
|-------|----------------------|
| Blackview | `Blackview rugged phone review` |
| Ulefone | `Ulefone Armor review` |
| Doogee | `Doogee rugged phone review` |
| Oukitel | `Oukitel rugged phone review` |
| Unihertz | `Unihertz rugged phone review` |
| FOSSiBOT | `FOSSiBOT rugged phone review` |
| Oscal | `Oscal rugged phone review` |
| HOTWAV | `HOTWAV rugged phone review` |
| RugOne (own brand) | `RugOne rugged phone review` |

You can add, edit, or remove brands via the Brands API or the Settings page.

---

## 🤝 Contributing

Contributions are welcome! To contribute:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Commit your changes (`git commit -m 'Add some feature'`)
4. Push to the branch (`git push origin feature/your-feature`)
5. Open a Pull Request

### Development Guidelines

- Follow PEP 8 style guidelines for Python code
- Test crawling and analysis with real data before submitting
- Ensure new API endpoints are documented in this README
- Update the database schema documentation if you modify table structures

---

## 📄 License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

```
MIT License

Copyright (c) 2026 RugOne Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions.

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
```

---

<p align="center">
  Built with care by the <strong>RugOne Team</strong>
</p>
