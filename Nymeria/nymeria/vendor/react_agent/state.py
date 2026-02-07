"""
State Schema for the ReAct Agent

The State is the data contract between nodes. Every node receives the current
state and returns updates to it. The checkpointer saves state after each node.

Extending State:
    Frameworks can create custom state by inheriting from AgentState:

    class MyState(AgentState):
        user_id: str
        context: dict

    Then use it with create_graph by passing a custom StateGraph.
"""

from typing import Annotated, TypedDict, Optional, Dict, Any, List
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """
    Base state for the ReAct agent.

    The `add_messages` reducer appends new messages instead of replacing them.
    This is critical for maintaining conversation history including tool calls.

    Attributes:
        messages: Conversation history (HumanMessage, AIMessage, ToolMessage)
    """
    messages: Annotated[List[BaseMessage], add_messages]


class ExtendedAgentState(AgentState):
    """
    Extended state with common fields frameworks often need.

    Use this as a starting point for custom state schemas.

    Attributes:
        messages: Conversation history
        user_id: Optional user identifier for multi-tenant apps
        session_metadata: Optional dict for session-specific data
        tool_outputs: Optional cache for expensive tool results
    """
    user_id: Optional[str]
    session_metadata: Optional[Dict[str, Any]]
    tool_outputs: Optional[Dict[str, Any]]


# Type alias for cleaner imports
State = AgentState
