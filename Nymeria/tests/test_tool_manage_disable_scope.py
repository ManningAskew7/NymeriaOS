"""Regression: the tool_manage(action="disable") guard protects the thread's
ACTUAL default-bound tools, not the raw SEED_TOOLS list.

Before the fix the guard classified "core" against SEED_TOOLS, so a user who
promoted an optional tool into their defaults got no protection for it, while a
seed tool they demoted out of their defaults (not even bound to the thread) was
still "protected". The guard now uses the same source graph-build does:
default_thread_tools, falling back to the seed set when uninitialized.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nymeria.core.agent import set_current_agent
from nymeria.core.thread_config import ThreadConfigManager
from nymeria.tools.tool_search import _disable


def _agent(tmp_path: Path, default_thread_tools):
    """Minimal agent surface used by _disable."""
    profile = SimpleNamespace(
        tool_preferences=SimpleNamespace(default_thread_tools=default_thread_tools)
    )
    return SimpleNamespace(
        accounts_repo=SimpleNamespace(get_thread_owner=lambda _tid: "default"),
        profile_manager=SimpleNamespace(get_profile=lambda _uid: profile),
        thread_config_manager=ThreadConfigManager(tmp_path / "threads"),
        invalidate_thread_config_cache=lambda _tid: None,
    )


def test_disable_protects_promoted_optional_not_demoted_seed(tmp_path: Path):
    # Customized defaults: a promoted optional tool is present; the seed tool
    # file_read has been demoted out.
    dtt = ["bash_execute", "memory_read", "web_search_tavily"]
    set_current_agent(_agent(tmp_path, dtt))
    try:
        # The promoted optional tool is now protected: refused without force.
        refused = _disable(["web_search_tavily"], "t1", force=False)
        assert "Refusing to disable default tool" in refused
        assert "web_search_tavily" in refused

        # The demoted seed tool is NOT in the thread's defaults, so it is not
        # protected: disabling proceeds instead of being wrongly refused.
        ok = _disable(["file_read"], "t1", force=False)
        assert ok.startswith("[Success]")
        assert "file_read" in ok
    finally:
        set_current_agent(None)


def test_disable_uninitialized_profile_protects_seed(tmp_path: Path):
    # default_thread_tools is None (uninitialized): the protected set falls back
    # to the seed names, so a seed tool is guarded exactly as before.
    set_current_agent(_agent(tmp_path, None))
    try:
        refused = _disable(["bash_execute"], "t2", force=False)
        assert "Refusing to disable default tool" in refused
        assert "bash_execute" in refused
    finally:
        set_current_agent(None)
