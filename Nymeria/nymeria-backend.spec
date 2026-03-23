# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Nymeria backend.

Builds a single nymeria-backend.exe that accepts the same CLI args as run.py.
The exe reads .env and data/ from NYMERIA_PROJECT_ROOT (set by Tauri launcher).

Build: cd Nymeria && pyinstaller nymeria-backend.spec
Output: Nymeria/dist/nymeria-backend.exe
"""

import os
from pathlib import Path

block_cipher = None

# Collect all nymeria submodules (PyInstaller misses dynamic imports)
nymeria_root = Path("nymeria")
nymeria_hidden = []
for py_file in nymeria_root.rglob("*.py"):
    module = str(py_file.with_suffix("")).replace(os.sep, ".")
    if module != "nymeria.__pycache__":
        nymeria_hidden.append(module)

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=[
        # Bundle soul.md as fallback (normally resolved via PROJECT_ROOT)
        ("nymeria/config/soul.md", "nymeria/config"),
    ],
    hiddenimports=[
        # All nymeria submodules (dynamic imports via importlib)
        *nymeria_hidden,
        # Uvicorn internals (not auto-detected)
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        # LangChain/LangGraph
        "langchain_anthropic",
        "langchain_openai",
        "langgraph",
        "langgraph.checkpoint",
        "langgraph_checkpoint_sqlite",
        # Database
        "aiosqlite",
        "sqlite_vec",
        # FastAPI / SSE
        "sse_starlette",
        "python_multipart",
        "multipart",
        # Google APIs
        "google.auth",
        "google.auth.transport",
        "google.auth.transport.requests",
        "google.auth.exceptions",
        "google.oauth2",
        "google.oauth2.credentials",
        "google_auth_oauthlib",
        "googleapiclient",
        "googleapiclient.discovery",
        # MCP
        "mcp",
        # Discord
        "discord",
        # Windows
        "win32timezone",
        "win32service",
        "win32serviceutil",
        "win32api",
        "win32event",
        "servicemanager",
        # Async engine
        "engineio.async_drivers.threading",
        # Pydantic
        "pydantic_settings",
        # Rich (CLI display)
        "rich",
        "prompt_toolkit",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy optional deps not needed for desktop
        "playwright",
        "psycopg",
        "redis",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Collect all MCP package data (templates, schemas)
from PyInstaller.utils.hooks import collect_all
mcp_datas, mcp_binaries, mcp_hidden = collect_all("mcp")
a.datas += mcp_datas
a.binaries += mcp_binaries
a.hiddenimports += mcp_hidden

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="nymeria-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Console app (no GUI window)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
