"""
Nymeria - Personal AI Assistant Framework

A 24/7 personalized AI assistant built on top of LangGraph ReAct core.
"""

from .core import NymeriaAgent
from .config import Settings, get_settings
from .tools import ALL_TOOLS, bash_execute, file_read, file_write, web_search
from .triggers import CLITrigger, create_api_app

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
    "web_search",
    # Triggers
    "CLITrigger",
    "create_api_app",
]
