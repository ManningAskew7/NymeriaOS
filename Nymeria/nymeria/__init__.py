"""
Nymeria - Personal AI Assistant Framework

A 24/7 personalized AI assistant built on top of LangGraph ReAct core.
"""

from ._runtime_paths import configure_project_root

configure_project_root()

__version__ = "0.1.0"
__all__ = [
    # Core
    "NymeriaAgent",
    # Config
    "Settings",
    "get_settings",
    # Tools
    "ALL_TOOLS",
    "bash_execute",
    "file_read",
    "file_write",
    "web_search_perplexity",
    # Triggers
    "CLITrigger",
    "create_api_app",
]


def __getattr__(name: str):
    """Lazily expose common package symbols without heavy import-time side effects."""
    if name == "NymeriaAgent":
        from .core import NymeriaAgent

        return NymeriaAgent
    if name in {"Settings", "get_settings"}:
        from .config import Settings, get_settings

        return {"Settings": Settings, "get_settings": get_settings}[name]
    if name in {"ALL_TOOLS", "bash_execute", "file_read", "file_write", "web_search_perplexity"}:
        from .tools import ALL_TOOLS, bash_execute, file_read, file_write, web_search_perplexity

        return {
            "ALL_TOOLS": ALL_TOOLS,
            "bash_execute": bash_execute,
            "file_read": file_read,
            "file_write": file_write,
            "web_search_perplexity": web_search_perplexity,
        }[name]
    if name in {"CLITrigger", "create_api_app"}:
        from .triggers import CLITrigger, create_api_app

        return {"CLITrigger": CLITrigger, "create_api_app": create_api_app}[name]
    raise AttributeError(f"module 'nymeria' has no attribute {name!r}")
