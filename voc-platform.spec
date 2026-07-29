# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — VoC 痛点挖掘平台
单文件 exe，包含 FastAPI + pywebview + yt-dlp + Instaloader + 多 LLM 支持
"""

block_cipher = None

from PyInstaller.utils.hooks import collect_all, collect_submodules

# 收集 yt_dlp / webview / instaloader / praw 的全部依赖
yt_dlp_datas, yt_dlp_binaries, yt_dlp_hidden = collect_all('yt_dlp')
webview_datas, webview_binaries, webview_hidden = collect_all('webview')
instaloader_datas, instaloader_binaries, instaloader_hidden = collect_all('instaloader')
praw_datas, praw_binaries, praw_hidden = collect_all('praw')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=yt_dlp_binaries + webview_binaries + instaloader_binaries + praw_binaries,
    datas=[
        ('static', 'static'),
    ] + yt_dlp_datas + webview_datas + instaloader_datas + praw_datas,
    hiddenimports=[
        'langdetect',
        'uvicorn.logging',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
        'uvicorn.protocols.websockets.websockets_impl',
        'uvicorn.protocols.http.h11_impl',
        'openai',
        'google.generativeai',
        'praw',
        'prawcore',
        'instaloader',
        'requests',
    ] + collect_submodules('instaloader') + collect_submodules('praw') + collect_submodules('prawcore') + yt_dlp_hidden + webview_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'scipy', 'pandas', 'tkinter', 'playwright', 'selenium'],
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
    name='VoC-Platform',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    icon='app_icon.ico',
)
