"""
Graph Nodes for the ReAct Agent

Modular node creation that accepts configuration for easy framework integration.
"""

import asyncio
import concurrent.futures
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

        # Summary line at INFO (always visible)
        tool_rounds = sum(1 for m in messages if isinstance(m, AIMessage) and m.tool_calls)
        logger.info(f"[LLM] Invoking with {len(messages)} messages ({tool_rounds} tool rounds)")

        # Detailed message dump at DEBUG (visible with llm profile)
        if logger.isEnabledFor(logging.DEBUG):
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
                logger.debug(f"[LLM]   [{i}] {msg_type}{tool_info}: {content_preview}...")

        # Prepend system prompt (not stored in state)
        messages_with_system = [SystemMessage(content=system_prompt)] + messages

        # Call the LLM
        response = llm_with_tools.invoke(messages_with_system)

        # Sanitize tool call names — some models emit leading/trailing whitespace
        # (e.g. ' CalendarAgent' instead of 'CalendarAgent') which breaks routing.
        if hasattr(response, 'tool_calls') and response.tool_calls:
            for tc in response.tool_calls:
                if 'name' in tc and tc['name'] != tc['name'].strip():
                    logger.warning(f"[LLM] Stripped whitespace from tool call name: {tc['name']!r} -> {tc['name'].strip()!r}")
                    tc['name'] = tc['name'].strip()

        # Log response summary at INFO
        resp_content = response.content if isinstance(response.content, str) else str(response.content)
        has_tools = bool(response.tool_calls) if hasattr(response, 'tool_calls') else False
        tool_names = [tc.get('name', '?') for tc in response.tool_calls] if has_tools else []
        logger.info(
            f"[LLM] Response: {len(resp_content)} chars"
            + (f", tool_calls={tool_names}" if has_tools else ", final answer")
        )

        # Check if response was truncated due to hitting max_tokens
        if hasattr(response, 'response_metadata'):
            finish_reason = response.response_metadata.get('finish_reason')
            if finish_reason == 'length':
                logger.warning(
                    f"[LLM] TRUNCATED — Response hit max_tokens limit "
                    f"(finish_reason='length', content_length={len(resp_content)}). "
                    f"The model's output was cut off mid-generation."
                )

        return {"messages": [response]}

    return agent_node


def create_tools_node(tools: List[BaseTool], handle_errors: bool = True, tool_timeout: Optional[int] = None, on_timeout: Optional[Callable] = None) -> "SafeToolNode":
    """
    Create a tools node that executes tool calls.

    Args:
        tools: List of tools the node can execute
        handle_errors: If True, catch tool exceptions and return error messages
                      instead of letting them bubble up
        tool_timeout: Seconds before a tool invocation is terminated (default 300)
        on_timeout: Optional callback invoked with the input dict when a timeout occurs

    Returns:
        SafeToolNode instance that handles errors and timeouts gracefully
    """
    return SafeToolNode(tools, handle_tool_errors=handle_errors, tool_timeout=tool_timeout, on_timeout=on_timeout)


class SafeToolNode(ToolNode):
    """
    A ToolNode wrapper that catches exceptions and returns them as tool results,
    and enforces a per-invocation timeout to prevent hanging tools from blocking
    the agent indefinitely.

    This prevents tool failures from crashing the entire agent and allows
    the LLM to see the error and potentially retry or handle it.
    """

    DEFAULT_TOOL_TIMEOUT = 300  # 5 minutes

    def __init__(self, tools: List[BaseTool], handle_tool_errors: bool = True, tool_timeout: Optional[int] = None, on_timeout: Optional[Callable] = None):
        super().__init__(tools, handle_tool_errors=handle_tool_errors)
        self._handle_errors = handle_tool_errors
        self._tool_timeout = tool_timeout if tool_timeout is not None else self.DEFAULT_TOOL_TIMEOUT
        self._on_timeout = on_timeout  # Optional callback: fn(input_dict) -> None

    def invoke(self, input, config=None, **kwargs):
        """Execute tools with a timeout to prevent indefinite hangs.

        Wraps the parent ToolNode.invoke() in a thread with a timeout.
        If the timeout fires, returns error ToolMessages for all pending
        tool calls so the agent can recover gracefully.

        Note: on timeout, the underlying thread may continue running in the
        background (Python cannot forcibly kill threads), but the agent is
        unblocked and can proceed. The executor is shut down with wait=False
        so the caller is not blocked by ThreadPoolExecutor cleanup.
        """
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(super().invoke, input, config, **kwargs)
        try:
            result = future.result(timeout=self._tool_timeout)
            executor.shutdown(wait=False)
            return result
        except concurrent.futures.TimeoutError:
            # shutdown(wait=False) returns immediately — the daemon worker
            # thread will finish on its own (or when the process exits).
            executor.shutdown(wait=False)
            if self._on_timeout:
                try:
                    self._on_timeout(input)
                except Exception as e:
                    logger.warning(f"on_timeout callback failed: {e}")
            return self._build_timeout_response(input)

    async def ainvoke(self, input, config=None, **kwargs):
        """Async tool execution with timeout."""
        try:
            return await asyncio.wait_for(
                super().ainvoke(input, config, **kwargs),
                timeout=self._tool_timeout,
            )
        except asyncio.TimeoutError:
            if self._on_timeout:
                try:
                    self._on_timeout(input)
                except Exception as e:
                    logger.warning(f"on_timeout callback failed: {e}")
            return self._build_timeout_response(input)

    def _build_timeout_response(self, input) -> dict:
        """Build error ToolMessages for timed-out tool calls.

        LangGraph requires a matching ToolMessage for every tool_call in the
        AIMessage, so we produce one error message per pending call.
        """
        messages = input.get("messages", []) if isinstance(input, dict) else []
        last_message = messages[-1] if messages else None

        error_messages = []
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            tool_names = [tc.get("name", "unknown") for tc in last_message.tool_calls]
            logger.error(
                f"Tool execution timed out after {self._tool_timeout}s. "
                f"Tools: {tool_names}"
            )
            for tc in last_message.tool_calls:
                tool_name = tc.get("name", "unknown")
                tool_call_id = tc.get("id", "unknown")
                error_messages.append(ToolMessage(
                    content=(
                        f"[Error]: Tool '{tool_name}' timed out after {self._tool_timeout} seconds. "
                        f"The operation took too long and was stopped to prevent the agent from hanging. "
                        f"Do NOT retry this tool — report the timeout to the user."
                    ),
                    tool_call_id=tool_call_id,
                ))
        else:
            logger.error(
                f"Tool execution timed out after {self._tool_timeout}s "
                f"but could not extract tool calls from input to build error response."
            )

        return {"messages": error_messages}


def create_should_continue(max_iterations: int = 10) -> Callable[[AgentState], str]:
    """
    Factory for the routing function with iteration limit.

    The iteration count is derived from the message history (counting tool calls
    since the last HumanMessage) rather than using a closure, making it
    thread-safe and stateless. Only the current turn's tool calls count toward
    the limit, so previous turns don't block future ones.

    Args:
        max_iterations: Maximum ReAct loops per turn before forcing end

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
            # Count tool calls only in the CURRENT TURN (since the last HumanMessage).
            # This prevents previous turns' tool calls from blocking future turns.
            current_turn_messages = []
            for msg in reversed(messages):
                if isinstance(msg, HumanMessage):
                    break
                current_turn_messages.append(msg)

            tool_call_count = sum(
                1 for msg in current_turn_messages
                if isinstance(msg, AIMessage) and msg.tool_calls
            )

            if tool_call_count > max_iterations:
                # Force stop to prevent infinite loops
                logger.warning(
                    f"Iteration limit reached ({tool_call_count}/{max_iterations}). "
                    f"Forcing agent to stop. The agent wanted to call more tools but was cut off."
                )
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
        return create_tools_node(
            self.tools,
            tool_timeout=self.config.tool_timeout,
            on_timeout=self.config.on_timeout,
        )

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
