"""Callable thread tools for Nymeria.

The old sub-agent system (AVAILABLE_AGENTS, register_agent, SubAgentExecutor) has been
replaced by callable threads. Any thread with callable=True becomes a directly
invocable tool via tool_factory.create_callable_thread_tool().
"""

from .tool_factory import create_callable_thread_tool, get_callable_thread_tools

__all__ = [
    "create_callable_thread_tool",
    "get_callable_thread_tools",
]
