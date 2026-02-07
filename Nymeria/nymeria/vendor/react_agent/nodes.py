"""
Graph Nodes for the ReAct Agent

Modular node creation that accepts configuration for easy framework integration.
"""

import logging
from typing import List, Callable, Optional
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.language_models import BaseChatModel
from langgraph.prebuilt import ToolNode

from .state import AgentState
from .config import AgentConfig, default_config
from .providers import create_llm_with_tools

logger = logging.getLogger(__name__)


def create_agent_node(
    llm_with_tools: BaseChatModel,
    system_prompt: str,
) -> Callable[[AgentState], dict]:
    """
    Factory function to create an agent node with custom LLM and prompt.

    Args:
        llm_with_tools: LLM with tools already bound
        system_prompt: System prompt for the agent

    Returns:
        Agent node function compatible with LangGraph
    """
    def agent_node(state: AgentState) -> dict:
        """
        The 'reasoning' node - asks the LLM what to do next.

        Returns AIMessage that either:
        - Has content (final answer to user)
        - Has tool_calls (instructions to call tools)
        - Has both (explaining what it's about to do)
        """
        messages = state["messages"]

        # DEBUG: Log exactly what messages are being sent to LLM
        logger.info(f"[LLM CALL DEBUG] Sending {len(messages)} messages to LLM (+ system prompt)")
        for i, msg in enumerate(messages):
            msg_type = type(msg).__name__
            content_preview = ""
            if hasattr(msg, 'content') and msg.content:
                content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
                content_preview = content_str[:150].replace('\n', ' ')
            tool_info = ""
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                tool_names = [tc.get('name', '?') for tc in msg.tool_calls]
                tool_info = f" [tools: {', '.join(tool_names)}]"
            logger.info(f"[LLM CALL DEBUG]   [{i}] {msg_type}{tool_info}: {content_preview}...")

        # Prepend system prompt (not stored in state)
        messages_with_system = [SystemMessage(content=system_prompt)] + messages

        # Call the LLM
        response = llm_with_tools.invoke(messages_with_system)

        return {"messages": [response]}

    return agent_node


def create_tools_node(tools: List[BaseTool], handle_errors: bool = True) -> "SafeToolNode":
    """
    Create a tools node that executes tool calls.

    Args:
        tools: List of tools the node can execute
        handle_errors: If True, catch tool exceptions and return error messages
                      instead of letting them bubble up

    Returns:
        SafeToolNode instance that handles errors gracefully
    """
    return SafeToolNode(tools, handle_tool_errors=handle_errors)


class SafeToolNode(ToolNode):
    """
    A ToolNode wrapper that catches exceptions and returns them as tool results.

    This prevents tool failures from crashing the entire agent and allows
    the LLM to see the error and potentially retry or handle it.
    """

    def __init__(self, tools: List[BaseTool], handle_tool_errors: bool = True):
        super().__init__(tools, handle_tool_errors=handle_tool_errors)
        self._handle_errors = handle_tool_errors


def create_should_continue(max_iterations: int = 10) -> Callable[[AgentState], str]:
    """
    Factory for the routing function with iteration limit.

    The iteration count is derived from the message history (counting tool calls)
    rather than using a closure, making it thread-safe and stateless.

    Args:
        max_iterations: Maximum ReAct loops before forcing end

    Returns:
        Routing function for conditional edges
    """
    def should_continue(state: AgentState) -> str:
        """
        Conditional edge: decides what node to go to next.

        Returns:
            'tools' if agent wants to use tools
            'end' if agent is done or iteration limit reached
        """
        messages = state["messages"]
        last_message = messages[-1]

        # Check if LLM wants to call tools
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            # Count tool calls in this conversation by counting AIMessages with tool_calls
            tool_call_count = sum(
                1 for msg in messages
                if isinstance(msg, AIMessage) and msg.tool_calls
            )

            if tool_call_count > max_iterations:
                # Force stop to prevent infinite loops
                return "end"

            return "tools"

        return "end"

    return should_continue


def simple_should_continue(state: AgentState) -> str:
    """
    Simple routing function without iteration tracking.

    For use when you want basic routing without limits,
    or when you're managing iteration limits externally.
    """
    messages = state["messages"]
    last_message = messages[-1]

    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"

    return "end"


class NodeFactory:
    """
    Factory class for creating all nodes from a single configuration.

    Usage:
        factory = NodeFactory(config, tools)
        agent = factory.create_agent_node()
        tools_node = factory.create_tools_node()
        router = factory.create_router()
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        tools: Optional[List[BaseTool]] = None,
    ):
        self.config = config or default_config
        self.tools = tools or []
        self._llm_with_tools = None

    @property
    def llm_with_tools(self) -> BaseChatModel:
        """Lazily create and cache the LLM with tools."""
        if self._llm_with_tools is None:
            self._llm_with_tools = create_llm_with_tools(
                self.config.llm,
                self.tools
            )
        return self._llm_with_tools

    def create_agent_node(self) -> Callable[[AgentState], dict]:
        """Create the agent reasoning node."""
        return create_agent_node(
            self.llm_with_tools,
            self.config.system_prompt
        )

    def create_tools_node(self) -> ToolNode:
        """Create the tool execution node."""
        return create_tools_node(self.tools)

    def create_router(self) -> Callable[[AgentState], str]:
        """Create the routing function."""
        return create_should_continue(self.config.max_iterations)


# === BACKWARD COMPATIBILITY ===
# These maintain compatibility with the old interface

from .tools import TOOLS
from dotenv import load_dotenv

load_dotenv()

# Create default nodes using default config
_default_factory = NodeFactory(default_config, TOOLS)

# Legacy exports (use NodeFactory for new code)
agent_node = _default_factory.create_agent_node()
tools_node = _default_factory.create_tools_node()
should_continue = simple_should_continue  # Use simple version for backward compat
