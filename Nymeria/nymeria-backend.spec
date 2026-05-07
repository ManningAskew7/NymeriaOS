# -*- mode: python ; coding: utf-8 -*-
# ruff: noqa: F821
"""PyInstaller spec for Nymeria backend.

Builds a single nymeria-backend.exe that accepts the same CLI args as run.py.
If NYMERIA_PROJECT_ROOT is set, the exe reads config and data from that root
(the Tauri launcher sets it for source-checkout desktop builds). Otherwise,
frozen launches use the packaged runtime convention: ~/.nymeria/config.env and
~/.nymeria/data/.

Build: cd Nymeria && pyinstaller nymeria-backend.spec
Output: Nymeria/dist/nymeria-backend.exe
"""

import os
import importlib.util
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

block_cipher = None

# Collect all nymeria submodules (PyInstaller misses dynamic imports used by
# tool hot-loading, router factories, and bot cogs).
nymeria_root = Path("nymeria")
nymeria_hidden = set(collect_submodules("nymeria", on_error="ignore"))
for py_file in nymeria_root.rglob("*.py"):
    module = str(py_file.with_suffix("")).replace(os.sep, ".")
    if module != "nymeria.__pycache__":
        nymeria_hidden.add(module)

# Package data that must exist next to the frozen nymeria package at runtime.
# settings.soul_path, bundled skills, and API frontend hosting all resolve from
# PACKAGE_ROOT, which PyInstaller maps to the extraction directory.
nymeria_datas = collect_data_files(
    "nymeria",
    includes=[
        "agents/*.md",
        "config/*.md",
        "frontend/**/*",
        "skills_bundled/**/*",
        "triggers/**/*.md",
        "vendor/react_agent/*.md",
    ],
)


def _safe_copy_metadata(package_name):
    try:
        return copy_metadata(package_name, recursive=True)
    except Exception:
        return []


metadata_datas = []
for package_name in (
    "anthropic",
    "langchain",
    "langchain-anthropic",
    "langchain-core",
    "langchain-openai",
    "langgraph",
    "langgraph-checkpoint-sqlite",
    "mcp",
    "openai",
    "pydantic",
    "pydantic-settings",
):
    metadata_datas += _safe_copy_metadata(package_name)

# Collect all MCP package data (templates, schemas).
mcp_datas, mcp_binaries, mcp_hidden = collect_all("mcp")


def _hidden_if_available(*module_names):
    available = []
    for name in module_names:
        try:
            if importlib.util.find_spec(name) is not None:
                available.append(name)
        except ModuleNotFoundError:
            continue
    return available


optional_hidden = _hidden_if_available(
    # Windows service helpers are present in Windows build environments with pywin32.
    "win32timezone",
    "win32service",
    "win32serviceutil",
    "win32api",
    "win32event",
    "servicemanager",
    # Socket.IO async driver is only needed when python-engineio is installed.
    "engineio.async_drivers.threading",
)

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=mcp_binaries,
    datas=nymeria_datas + metadata_datas + mcp_datas,
    hiddenimports=[
        # All nymeria submodules (dynamic imports via importlib)
        *sorted(nymeria_hidden),
        *mcp_hidden,
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
        "langgraph.checkpoint.sqlite",
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
        "google.oauth2.service_account",
        "google.genai",
        "google.genai.types",
        "google_auth_oauthlib",
        "googleapiclient",
        "googleapiclient.discovery",
        "googleapiclient.errors",
        # Provider SDKs and optional tool deps imported lazily
        "anthropic",
        "openai",
        "feedparser",
        "openpyxl",
        # MCP
        "mcp",
        # Discord
        "discord",
        # Telegram / Twitch
        "telegram",
        "telegram.ext",
        "twitchio",
        # Optional/platform-specific imports present in some build envs.
        *optional_hidden,
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
