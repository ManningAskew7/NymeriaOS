"""Shared utility functions for Nymeria tools."""

from typing import Optional

from langchain_core.runnables import RunnableConfig


def get_user_id(config: Optional[RunnableConfig]) -> str:
    """
    Extract user_id from RunnableConfig, defaulting to 'default'.

    Args:
        config: LangChain runnable config containing configurable options

    Returns:
        User ID string, defaults to 'default' if not found
    """
    if config is None:
        return "default"
    return config.get("configurable", {}).get("user_id", "default")


def get_thread_id(config: Optional[RunnableConfig]) -> str:
    """
    Extract thread_id from RunnableConfig, defaulting to 'default'.

    Args:
        config: LangChain runnable config containing configurable options

    Returns:
        Thread ID string, defaults to 'default' if not found
    """
    if config is None:
        return "default"
    return config.get("configurable", {}).get("thread_id", "default")
