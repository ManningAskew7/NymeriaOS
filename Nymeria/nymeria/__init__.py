"""
Nymeria - Personal AI Assistant Framework

A 24/7 personalized AI assistant built on top of LangGraph ReAct core.
"""

from typing import TYPE_CHECKING

from ._runtime_paths import configure_project_root

configure_project_root()

if TYPE_CHECKING:
    # Import eagerly for type checkers and IDEs only. At runtime these resolve
    # through __getattr__ below, so nothing here costs import time.
    from .core import NymeriaAgent
    from .config import Settings, get_settings
    from .tools import (
        SEED_TOOLS,
        bash_execute,
        file_read,
        file_write,
        web_search_perplexity,
    )
    from .triggers import CLITrigger, create_api_app

__version__ = "0.2.0-beta.1"
__all__ = [
    # Core
    "NymeriaAgent",
    # Config
    "Settings",
    "get_settings",
    # Tools
    "SEED_TOOLS",
    "bash_execute",
    "file_read",
    "file_write",
    "web_search_perplexity",
    # Triggers
    "CLITrigger",
    "create_api_app",
]


# Lazily expose common package symbols without heavy import-time side effects.
# Same PEP 562 shape as `core/`, `triggers/`, and `vendor/react_agent/`: one
# name -> submodule table, so there is a single idiom to learn and no chain of
# `if` branches rebuilding a throwaway dict on every attribute access.
_LAZY_EXPORTS = {
    "NymeriaAgent": ".core",
    "Settings": ".config",
    "get_settings": ".config",
    "SEED_TOOLS": ".tools",
    "bash_execute": ".tools",
    "file_read": ".tools",
    "file_write": ".tools",
    "web_search_perplexity": ".tools",
    "CLITrigger": ".triggers",
    "create_api_app": ".triggers",
}


def __getattr__(name: str):
    """Resolve a re-exported symbol by importing only its defining submodule.

    Unlike the sibling packages this does NOT fall back to importing ``name``
    as a submodule. Those inits used to bind submodules eagerly, so the
    fallback restores behaviour there; this one never did (its only eager
    import was ``._runtime_paths``), so a fallback would be a new surface, and
    an expensive one: it would turn a cheap ``hasattr(nymeria, "tools")``
    False into an 8.5s import that loads langchain. Import subpackages
    directly.
    """
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    return getattr(import_module(module, __name__), name)


def __dir__() -> list[str]:
    # Augment the default (`vars()`), never replace it, so `__version__` and
    # already-imported subpackages stay visible without a special case.
    return sorted(set(__all__) | set(globals()))
