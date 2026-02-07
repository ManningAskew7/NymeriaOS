"""
ReAct Agent Package

A modular LangGraph ReAct agent designed for easy framework integration.

Quick Start:
    from react_agent import ReactAgent

    agent = ReactAgent()
    response = agent.chat("Hello!", thread_id="user-123")

Framework Integration:
    from react_agent import (
        AgentConfig, LLMConfig, CheckpointerConfig,
        create_graph, ToolRegistry
    )

    # Custom configuration
    config = AgentConfig(
        llm=LLMConfig(provider="anthropic", model="claude-3-5-sonnet"),
        system_prompt="You are a coding assistant.",
    )

    # Custom tools
    registry = ToolRegistry()
    registry.register(my_tool)

    # Create graph
    graph = create_graph(config=config, tools=registry.get_enabled_tools())
"""

# Configuration
from .config import (
    AgentConfig,
    LLMConfig,
    CheckpointerConfig,
    default_config,
)

# Tool management
from .tool_registry import (
    ToolRegistry,
    default_registry,
    get_default_registry,
    register_tool,
)

# LLM providers
from .providers import (
    create_llm,
    create_llm_with_tools,
    get_preset,
    PROVIDER_PRESETS,
    MODELS_TOOL_COMPATIBLE,
    MODELS_WITH_TOOL_ISSUES,
    check_model_compatibility,
)

# State
from .state import AgentState, ExtendedAgentState, State

# Node factories
from .nodes import (
    NodeFactory,
    create_agent_node,
    create_tools_node,
    create_should_continue,
    simple_should_continue,
    # Legacy exports
    agent_node,
    tools_node,
    should_continue,
)

# Graph
from .graph import (
    create_graph,
    get_graph_with_memory,
    create_checkpointer,
    ReactAgent,
    # Default graph for LangGraph Studio
    graph,
)

# Built-in tools
from .tools import TOOLS

__all__ = [
    # Configuration
    "AgentConfig",
    "LLMConfig",
    "CheckpointerConfig",
    "default_config",
    # Tool management
    "ToolRegistry",
    "default_registry",
    "get_default_registry",
    "register_tool",
    "TOOLS",
    # Providers
    "create_llm",
    "create_llm_with_tools",
    "get_preset",
    "PROVIDER_PRESETS",
    "MODELS_TOOL_COMPATIBLE",
    "MODELS_WITH_TOOL_ISSUES",
    "check_model_compatibility",
    # State
    "AgentState",
    "ExtendedAgentState",
    "State",
    # Nodes
    "NodeFactory",
    "create_agent_node",
    "create_tools_node",
    "create_should_continue",
    "simple_should_continue",
    "agent_node",
    "tools_node",
    "should_continue",
    # Graph
    "create_graph",
    "get_graph_with_memory",
    "create_checkpointer",
    "ReactAgent",
    "graph",
]
