"""
ReAct Agent Package

A modular LangGraph ReAct agent designed for easy framework integration.

Framework Integration:
    from react_agent import (
        AgentConfig, LLMConfig, CheckpointerConfig,
        create_graph, ToolRegistry
    )

    # Custom configuration
    config = AgentConfig(
        llm=LLMConfig(provider="anthropic", model="claude-sonnet-4-6"),
        system_prompt="You are a coding assistant.",
    )

    # Custom tools
    registry = ToolRegistry()
    registry.register(my_tool)

    # Create graph
    graph = create_graph(config=config, tools=registry.get_enabled_tools())
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Import eagerly for type checkers and IDEs only. At runtime these resolve
    # through __getattr__ below, so nothing here costs import time.
    from .config import (
        AgentConfig,
        LLMFallbackConfig,
        LLMConfig,
        CheckpointerConfig,
        default_config,
    )
    from .tool_registry import ToolRegistry
    from .providers import create_llm, create_llm_with_tools
    from .state import AgentState, State
    from .nodes import (
        NodeFactory,
        create_agent_node,
        create_tools_node,
        create_should_continue,
        analyze_turn_safety,
        TurnSafetyResult,
        simple_should_continue,
    )
    from .graph import create_graph, create_checkpointer

# Re-exports are resolved lazily (PEP 562), mirroring `nymeria/__init__.py`.
# Importing this package eagerly used to pull `.providers`, and through it
# langchain_openai -> langchain_core -> transformers -> torch, costing ~6.4s.
# That is dead weight for every caller that only wants a config dataclass or
# the cliproxy helpers, notably the thin CLI. Measured per submodule:
# .config 19ms, .tool_registry 892ms, .state 921ms, .providers 6431ms,
# .nodes 6480ms, .graph 6921ms.
_LAZY_EXPORTS = {
    "AgentConfig": ".config",
    "LLMFallbackConfig": ".config",
    "LLMConfig": ".config",
    "CheckpointerConfig": ".config",
    "default_config": ".config",
    "ToolRegistry": ".tool_registry",
    "create_llm": ".providers",
    "create_llm_with_tools": ".providers",
    "AgentState": ".state",
    "State": ".state",
    "NodeFactory": ".nodes",
    "create_agent_node": ".nodes",
    "create_tools_node": ".nodes",
    "create_should_continue": ".nodes",
    "analyze_turn_safety": ".nodes",
    "TurnSafetyResult": ".nodes",
    "simple_should_continue": ".nodes",
    "create_graph": ".graph",
    "create_checkpointer": ".graph",
}


def __getattr__(name: str):
    """Resolve a re-exported symbol by importing only its defining submodule.

    Falls back to importing ``name`` as a submodule, because the eager imports
    this replaced also bound every submodule they touched as a package
    attribute (``react_agent.graph``). Dropping that silently broke
    ``tests/test_react_agent_import_surface.py``.
    """
    from importlib import import_module

    module = _LAZY_EXPORTS.get(name)
    if module is not None:
        return getattr(import_module(module, __name__), name)

    try:
        return import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        # Only translate "this submodule does not exist". A submodule that
        # exists but fails on a missing dependency must surface its own error,
        # not be masked as a missing attribute.
        if exc.name != f"{__name__}.{name}":
            raise
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None


def __dir__() -> list[str]:
    # Augment the default (`vars()`), never replace it: __getattr__ makes
    # submodules reachable, so hiding them here (along with __name__ and
    # already-imported submodules) would be a strict regression.
    return sorted(set(__all__) | set(globals()))


__all__ = [
    # Configuration
    "AgentConfig",
    "LLMFallbackConfig",
    "LLMConfig",
    "CheckpointerConfig",
    "default_config",
    # Tool management
    "ToolRegistry",
    # Providers
    "create_llm",
    "create_llm_with_tools",
    # State
    "AgentState",
    "State",
    # Nodes
    "NodeFactory",
    "create_agent_node",
    "create_tools_node",
    "create_should_continue",
    "analyze_turn_safety",
    "TurnSafetyResult",
    "simple_should_continue",
    # Graph
    "create_graph",
    "create_checkpointer",
]
