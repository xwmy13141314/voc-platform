# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller definition for the Windows single-file desktop build."""

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

yt_dlp_datas, yt_dlp_binaries, yt_dlp_hidden = collect_all("yt_dlp")
webview_datas, webview_binaries, webview_hidden = collect_all("webview")
instaloader_datas, instaloader_binaries, instaloader_hidden = collect_all("instaloader")
jiter_datas, jiter_binaries, jiter_hidden = collect_all("jiter")
pydantic_datas, pydantic_binaries, pydantic_hidden = collect_all("pydantic_core")
fb_datas, fb_binaries, fb_hidden = collect_all("facebook_scraper")
req_html_datas, req_html_binaries, req_html_hidden = collect_all("requests_html")
bc3_datas, bc3_binaries, bc3_hidden = collect_all("browser_cookie3")
praw_datas, praw_binaries, praw_hidden = collect_all("praw")
prawcore_datas, prawcore_binaries, prawcore_hidden = collect_all("prawcore")

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=(
        yt_dlp_binaries + webview_binaries + instaloader_binaries
        + jiter_binaries + pydantic_binaries
        + fb_binaries + req_html_binaries + bc3_binaries
        + praw_binaries + prawcore_binaries
    ),
    datas=(
        [("static", "static")]
        + yt_dlp_datas + webview_datas + instaloader_datas
        + jiter_datas + pydantic_datas
        + fb_datas + req_html_datas + bc3_datas
        + praw_datas + prawcore_datas
    ),
    hiddenimports=[
        "langdetect",
        "uvicorn.logging",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.http.h11_impl",
        "openai",
        "jiter",
        "jiter.jiter",
        "pydantic_core",
        "pydantic_core._pydantic_core",
        "instaloader",
        "requests",
        "facebook_scraper",
        "requests_html",
        "lxml_html_clean",
        "dateparser",
        "demjson3",
        "pyquery",
        "fake_useragent",
        "parse",
        "w3lib",
        "cssselect",
        "bs4",
        "browser_cookie3",
        "praw",
        "prawcore",
        "pycryptodome",
        "pyaes",
        "keyring",
    ] + collect_submodules("instaloader") + yt_dlp_hidden + webview_hidden
      + instaloader_hidden + jiter_hidden + pydantic_hidden
      + fb_hidden + req_html_hidden + bc3_hidden
      + praw_hidden + prawcore_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 桌面精简版不内置聚类科学计算栈（约 1GB+，单文件打包不现实）：
    # clustering.py 对这些包做了惰性导入 + missing_clustering_deps 优雅降级，
    # 冻结版仍可查看/重命名现有簇，仅"重跑聚类"需源码模式。
    excludes=[
        "matplotlib", "numpy", "scipy", "pandas", "tkinter", "playwright", "selenium",
        "torch", "torchvision", "torchaudio",
        "sentence_transformers", "transformers", "tokenizers", "safetensors",
        "umap", "hdbscan", "sklearn", "numba", "llvmlite", "pynndescent",
        "huggingface_hub", "accelerate",
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="VoC-Platform",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon="app_icon.ico",
)
