"""
Nymeria MCP Server - Expose Nymeria's capabilities via Model Context Protocol.

This module creates an MCP server that allows other AI agents (like Claude Code)
to interact with Nymeria programmatically for:
- Autonomous chat conversations
- Memory management
- TODO task tracking
- RAG search across past conversations
- Thread history retrieval

Usage:
    python run.py mcp              # Start in STDIO mode (default)
    python run.py mcp --http       # Start in HTTP mode
    python run.py mcp --port 8001  # HTTP mode on custom port
"""

import asyncio
import logging
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Configure logging to stderr to avoid corrupting JSON-RPC in STDIO mode
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

# Create FastMCP server
# stateless_http=True allows each request to work independently without session management
mcp = FastMCP(
    "nymeria",
    instructions="Nymeria Personal AI Assistant - Chat, memory, TODOs, and RAG search",
    stateless_http=True,
)

# Global agent instance (lazily initialized)
_agent: Optional["NymeriaAgent"] = None


def _get_agent():
    """Get or initialize the global Nymeria agent."""
    global _agent
    if _agent is None:
        logger.info("Initializing Nymeria agent for MCP server...")
        from nymeria import NymeriaAgent
        from nymeria.tools import get_all_tools_with_agents
        _agent = NymeriaAgent(tools=get_all_tools_with_agents())
        _agent.sync_agent_tools()
        logger.info("Nymeria agent initialized successfully")
    return _agent


# =============================================================================
# Chat Tool
# =============================================================================

@mcp.tool()
async def nymeria_chat(
    message: str,
    user_id: str = "default",
    thread_id: str = None,
) -> str:
    """
    Send a message to Nymeria and get a response.

    Nymeria is a personal AI assistant that can:
    - Have natural conversations
    - Remember information about users
    - Execute tools (web search, file operations, etc.)
    - Manage TODO tasks

    Args:
        message: The message to send to Nymeria
        user_id: User ID for memory and personalization (default: "default")
        thread_id: Thread ID for conversation continuity (auto-generated if not provided)

    Returns:
        Nymeria's response text
    """
    if not message or not message.strip():
        return "Error: Please provide a message."

    agent = _get_agent()

    # Use a deterministic thread_id per user so conversations persist across calls.
    # Callers can still pass an explicit thread_id for separate conversations.
    if not thread_id:
        thread_id = f"mcp-{user_id}"

    logger.info(f"nymeria_chat: user={user_id}, thread={thread_id}, message={message[:50]}...")

    try:
        # Run blocking agent.chat() in a thread to avoid blocking the async event loop
        response = await asyncio.to_thread(
            agent.chat,
            message=message,
            thread_id=thread_id,
            user_id=user_id,
        )
        logger.info(f"nymeria_chat: response={response[:100]}...")
        return response
    except Exception as e:
        logger.error(f"nymeria_chat error: {e}", exc_info=True)
        return f"Error: {str(e)}"


# =============================================================================
# Profile Tools (formerly Memory)
# =============================================================================

@mcp.tool()
async def nymeria_profile_save(
    key: str,
    value: str,
    user_id: str = "default",
) -> str:
    """
    Save a memory about the user to Nymeria's persistent storage.

    Memories are automatically available in all future conversations
    with this user. They're injected into Nymeria's system prompt.

    Args:
        key: Category/identifier for the memory (e.g., "user_name", "occupation")
        value: The information to remember (max 1000 characters)
        user_id: User ID to save memory for (default: "default")

    Returns:
        Confirmation message

    Examples:
        nymeria_memory_save(key="user_name", value="Alex")
        nymeria_memory_save(key="preference_tone", value="casual and friendly")
    """
    if not key or not value:
        return "Error: Both key and value are required."

    agent = _get_agent()

    try:
        with agent.profile_manager.atomic_update(user_id) as profile:
            success = profile.add_memory(key, value)
            if success:
                logger.info(f"Memory saved: user={user_id}, key={key}")
                return f"Saved memory '{key}' for user '{user_id}'."
            else:
                return f"Error: Memory limit reached ({profile.MAX_MEMORIES}). Remove old memories first."
    except Exception as e:
        logger.error(f"nymeria_memory_save error: {e}", exc_info=True)
        return f"Error: {str(e)}"


@mcp.tool()
async def nymeria_profile_list(
    user_id: str = "default",
) -> str:
    """
    List all memories stored for a user.

    Returns all saved memories and personality preferences.

    Args:
        user_id: User ID to list memories for (default: "default")

    Returns:
        Formatted list of all stored memories
    """
    agent = _get_agent()

    try:
        profile = agent.profile_manager.get_profile(user_id)

        if not profile.memories and not profile.personality_overrides:
            return f"No memories stored for user '{user_id}'."

        lines = [f"Memories for user '{user_id}':"]

        if profile.memories:
            lines.append("\nFacts:")
            for mem in sorted(profile.memories, key=lambda m: m.key):
                lines.append(f"  - {mem.key}: {mem.value}")

        if profile.personality_overrides:
            lines.append("\nPersonality preferences:")
            for trait, value in profile.personality_overrides.items():
                lines.append(f"  - {trait}: {value}")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"nymeria_memory_list error: {e}", exc_info=True)
        return f"Error: {str(e)}"


@mcp.tool()
async def nymeria_profile_forget(
    key: str,
    user_id: str = "default",
) -> str:
    """
    Remove a memory from Nymeria's storage.

    Args:
        key: The memory key to delete
        user_id: User ID to remove memory from (default: "default")

    Returns:
        Confirmation message
    """
    if not key:
        return "Error: key is required."

    agent = _get_agent()

    try:
        with agent.profile_manager.atomic_update(user_id) as profile:
            success = profile.remove_memory(key)
            if success:
                logger.info(f"Memory removed: user={user_id}, key={key}")
                return f"Removed memory '{key}' for user '{user_id}'."
            else:
                keys = profile.list_memory_keys()
                if keys:
                    return f"Error: No memory found with key '{key}'. Available keys: {', '.join(keys)}"
                return "Error: No memories stored."
    except Exception as e:
        logger.error(f"nymeria_memory_forget error: {e}", exc_info=True)
        return f"Error: {str(e)}"


# =============================================================================
# RAG Search Tool
# =============================================================================

@mcp.tool()
async def nymeria_rag_search(
    query: str,
    max_results: int = 5,
    user_id: str = "default",
) -> str:
    """
    Search past conversations and memories for relevant context.

    Uses semantic search to find relevant information from:
    - Past conversation snippets
    - Saved memories
    - Completed TODO outcomes

    Args:
        query: What to search for (e.g., "previous discussions about databases")
        max_results: Maximum results to return (1-10, default: 5)
        user_id: User ID to search for (default: "default")

    Returns:
        Relevant context from past conversations and memories
    """
    if not query:
        return "Error: query is required."

    agent = _get_agent()

    try:
        # Check if RAG is enabled
        profile = agent.profile_manager.get_profile(user_id)
        if not profile.opt_in.rag_enabled:
            return f"RAG is not enabled for user '{user_id}'. Enable it via nymeria_chat first."

        # Get memory index
        memory_index = agent._get_memory_index(user_id)
        if not memory_index:
            return "Error: Could not access memory index."

        # Clamp max_results
        max_results = max(1, min(10, max_results))

        # Get RAG preferences
        rag_prefs = profile.get_rag_preferences()
        chunk_types = []
        if rag_prefs.get("include_conversations", True):
            chunk_types.append("conversation")
        if rag_prefs.get("include_memories", True):
            chunk_types.append("memory")
        if rag_prefs.get("include_todos", True):
            chunk_types.append("todo")

        if not chunk_types:
            return "All content types are disabled in RAG settings."

        # Search
        results = memory_index.search(
            query=query,
            user_id=user_id,
            limit=max_results,
            chunk_types=chunk_types,
        )

        if not results:
            return f"No relevant context found for '{query}'."

        # Format results
        lines = [f"Found {len(results)} result(s) for '{query}':\n"]
        for i, result in enumerate(results, 1):
            content = result.content
            if len(content) > 400:
                content = content[:397] + "..."
            lines.append(f"{i}. [{result.chunk_type}] (relevance: {result.score:.2f})")
            lines.append(f"   {content}")
            lines.append("")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"nymeria_rag_search error: {e}", exc_info=True)
        return f"Error: {str(e)}"


# =============================================================================
# TODO Tools
# =============================================================================

@mcp.tool()
async def nymeria_todo_list(
    user_id: str = "default",
    status: str = None,
) -> str:
    """
    List TODO items for a user.

    TODOs drive Nymeria's autonomous operation. Active TODOs are
    automatically shown to Nymeria in each conversation.

    Args:
        user_id: User ID to list TODOs for (default: "default")
        status: Filter by status - "pending", "in_progress", "done", or "all"

    Returns:
        Formatted list of TODO items
    """
    agent = _get_agent()

    try:
        todo_list_obj = agent.todo_manager.get_todos(user_id)

        # Import constants for formatting
        from nymeria.core.todo_constants import STATUS_ICONS, STATUS_ORDER
        from nymeria.core.todo_manager import TodoStatus

        # Filter items
        if status == "all":
            items = todo_list_obj.items
        elif status:
            try:
                filter_status = TodoStatus(status.lower())
                items = [i for i in todo_list_obj.items if i.status == filter_status]
            except ValueError:
                return f"Error: Invalid status '{status}'. Use 'pending', 'in_progress', 'done', or 'all'."
        else:
            items = todo_list_obj.get_active_todos()

        if not items:
            if status:
                return f"No TODOs with status '{status}' for user '{user_id}'."
            return f"No active TODOs for user '{user_id}'."

        # Sort: in_progress first, then pending, then done; then by created_at
        sorted_items = sorted(
            items,
            key=lambda i: (STATUS_ORDER.get(i.status, 3), i.created_at),
        )

        # Format output
        lines = [f"TODOs for user '{user_id}' ({len(items)} item(s)):"]
        lines.append("")

        for item in sorted_items:
            icon = STATUS_ICONS.get(item.status, "[ ]")
            line = f"{icon} [{item.id}] {item.task}"

            if item.scheduled_for:
                from nymeria.core.time_utils import get_user_tz
                display_time = item.scheduled_for.astimezone(get_user_tz())
                line += f" [scheduled: {display_time.strftime('%Y-%m-%d %H:%M')}]"

            lines.append(line)

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"nymeria_todo_list error: {e}", exc_info=True)
        return f"Error: {str(e)}"


@mcp.tool()
async def nymeria_todo_add(
    task: str,
    scheduled_for: str = None,
    recurrence: str = None,
    user_id: str = "default",
) -> str:
    """
    Add a new TODO item.

    TODOs drive Nymeria's autonomous operation. When scheduled_for is set,
    Nymeria will automatically wake up to work on the task at that time.

    Args:
        task: The task description
        scheduled_for: When Nymeria should work on this task.
                      Formats: "30s", "5m", "1h", "1d" (relative),
                      or "2024-03-15 14:00" (absolute)
        recurrence: Recurrence pattern - "5min", "10min", "15min", "30min", "hourly", "daily", "weekly", "monthly"
        user_id: User ID to add TODO for (default: "default")

    Returns:
        Confirmation message with the new TODO ID

    Examples:
        nymeria_todo_add(task="Research Python async patterns", scheduled_for="2h")
        nymeria_todo_add(task="Deploy feature", scheduled_for="1d", recurrence="daily")
    """
    if not task:
        return "Error: task is required."

    agent = _get_agent()

    try:
        from nymeria.core.time_utils import parse_scheduled_time
        from nymeria.core.todo_constants import VALID_RECURRENCES
        from nymeria.core.activity_log import ActivityType, log_activity

        # Parse scheduled_for
        todo_scheduled = None
        if scheduled_for:
            todo_scheduled = parse_scheduled_time(scheduled_for)
            if not todo_scheduled:
                return f"Error: Invalid scheduled_for format '{scheduled_for}'. Use '30s', '5m', '1h', '1d' or 'YYYY-MM-DD HH:MM'."

        # Validate recurrence
        todo_recurrence = None
        if recurrence:
            if recurrence.lower() not in VALID_RECURRENCES:
                return f"Error: Invalid recurrence '{recurrence}'. Use: {', '.join(VALID_RECURRENCES)}"
            todo_recurrence = recurrence.lower()

        with agent.todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.add_item(
                task,
                scheduled_for=todo_scheduled,
                recurrence=todo_recurrence,
            )
            if item:
                logger.info(f"TODO added: user={user_id}, id={item.id}")

                # Sync to schedule database if scheduled
                if todo_scheduled and hasattr(agent, '_schedule_db'):
                    agent._schedule_db.add_scheduled(
                        todo_id=item.id,
                        user_id=user_id,
                        scheduled_for=todo_scheduled,
                        task_preview=task[:100],
                    )

                # Log activity
                log_activity(
                    ActivityType.TODO_ADDED,
                    f"TODO added (MCP): {task[:80]}",
                    user_id=user_id,
                    metadata={"todo_id": item.id, "source": "mcp"},
                )

                result = f"Added TODO {item.id}: {task[:100]}"
                if todo_scheduled:
                    result += f" (scheduled for {scheduled_for})"
                if todo_recurrence:
                    result += f" (recurring: {todo_recurrence})"
                return result
            else:
                return f"Error: TODO limit reached ({todo_list.MAX_TODOS}). Complete or delete some tasks first."
    except Exception as e:
        logger.error(f"nymeria_todo_add error: {e}", exc_info=True)
        return f"Error: {str(e)}"


@mcp.tool()
async def nymeria_todo_complete(
    todo_id: str,
    user_id: str = "default",
) -> str:
    """
    Mark a TODO item as completed.

    Completed items are archived after 7 days. Any scheduled execution
    is automatically cancelled.

    Args:
        todo_id: The 8-character TODO ID to complete
        user_id: User ID (default: "default")

    Returns:
        Confirmation message
    """
    if not todo_id:
        return "Error: todo_id is required."

    agent = _get_agent()

    try:
        from nymeria.core.activity_log import ActivityType, log_activity
        from nymeria.core.todo_manager import TodoStatus

        with agent.todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                return f"Error: TODO '{todo_id}' not found."

            task_name = item.task
            has_recurrence = item.recurrence
            success = todo_list.complete_item(todo_id)
            if success:
                logger.info(f"TODO completed: user={user_id}, id={todo_id}")

                # Auto-reschedule recurring TODOs
                rescheduled = False
                if has_recurrence:
                    from nymeria.core.todo_constants import RECURRENCE_DELTAS
                    from datetime import datetime
                    delta = RECURRENCE_DELTAS.get(has_recurrence)
                    if delta:
                        next_execution = datetime.utcnow() + delta
                        todo_list.update_item(
                            todo_id,
                            scheduled_for=next_execution,
                            status=TodoStatus.PENDING,
                        )
                        refreshed = todo_list.get_item(todo_id)
                        if refreshed:
                            refreshed.last_execution = datetime.utcnow()
                        rescheduled = True
                        logger.info(f"Auto-rescheduled recurring TODO {todo_id} for {next_execution}")

                # Sync schedule database
                if hasattr(agent, '_schedule_db'):
                    if rescheduled:
                        agent.todo_manager.sync_schedule_to_db(
                            user_id, todo_id, agent._schedule_db
                        )
                    else:
                        agent._schedule_db.remove_scheduled(todo_id)

                # Log activity
                log_activity(
                    ActivityType.TODO_COMPLETED,
                    f"TODO completed (MCP): {task_name[:80]}",
                    user_id=user_id,
                    metadata={"todo_id": todo_id, "source": "mcp"},
                )

                if rescheduled:
                    return f"Completed: {task_name[:100]} (auto-rescheduled: recurring {has_recurrence})"
                return f"Completed: {task_name[:100]}"
            else:
                return f"Error: Failed to complete TODO '{todo_id}'."
    except Exception as e:
        logger.error(f"nymeria_todo_complete error: {e}", exc_info=True)
        return f"Error: {str(e)}"


@mcp.tool()
async def nymeria_todo_update(
    todo_id: str,
    task: str = None,
    status: str = None,
    notes: str = None,
    scheduled_for: str = None,
    clear_schedule: bool = False,
    user_id: str = "default",
) -> str:
    """
    Update a TODO item's fields.

    Args:
        todo_id: The 8-character TODO ID to update
        task: New task description (optional)
        status: New status - "pending", "in_progress", or "done" (optional)
        notes: Add notes to the TODO (max 1000 chars, optional)
        scheduled_for: New schedule - "30s", "5m", "1h", "1d" or "YYYY-MM-DD HH:MM" (optional)
        clear_schedule: Set to true to remove the scheduled time (optional)
        user_id: User ID (default: "default")

    Returns:
        Confirmation message
    """
    if not todo_id:
        return "Error: todo_id is required."

    agent = _get_agent()

    try:
        from nymeria.core.time_utils import parse_scheduled_time
        from nymeria.core.todo_manager import TodoStatus
        from nymeria.core.activity_log import ActivityType, log_activity

        # Parse status
        todo_status = None
        if status:
            try:
                todo_status = TodoStatus(status.lower())
            except ValueError:
                return f"Error: Invalid status '{status}'. Use 'pending', 'in_progress', or 'done'."

        # Parse scheduled_for
        todo_scheduled = None
        if scheduled_for and not clear_schedule:
            todo_scheduled = parse_scheduled_time(scheduled_for)
            if not todo_scheduled:
                return f"Error: Invalid scheduled_for format '{scheduled_for}'. Use '30s', '5m', '1h', '1d' or 'YYYY-MM-DD HH:MM'."

        with agent.todo_manager.atomic_update(user_id) as todo_list:
            success = todo_list.update_item(
                todo_id,
                task=task,
                status=todo_status,
                notes=notes,
                scheduled_for=todo_scheduled,
                clear_schedule=clear_schedule,
            )
            if success:
                item = todo_list.get_item(todo_id)
                logger.info(f"TODO updated: user={user_id}, id={todo_id}")

                # Sync to schedule database
                if hasattr(agent, '_schedule_db'):
                    if clear_schedule or todo_status == TodoStatus.DONE:
                        agent._schedule_db.remove_scheduled(todo_id)
                    elif todo_scheduled:
                        agent._schedule_db.add_scheduled(
                            todo_id=item.id,
                            user_id=user_id,
                            scheduled_for=todo_scheduled,
                            task_preview=item.task[:100],
                        )

                # Log activity
                log_activity(
                    ActivityType.TODO_UPDATED,
                    f"TODO updated (MCP): {item.task[:60]} (status: {item.status.value})",
                    user_id=user_id,
                    metadata={"todo_id": todo_id, "status": item.status.value, "source": "mcp"},
                )

                result = f"Updated TODO {todo_id}: {item.task[:50]} (status: {item.status.value})"
                if item.scheduled_for:
                    result += " (scheduled)"
                elif clear_schedule:
                    result += " (schedule cleared)"
                return result
            else:
                return f"Error: TODO '{todo_id}' not found."
    except Exception as e:
        logger.error(f"nymeria_todo_update error: {e}", exc_info=True)
        return f"Error: {str(e)}"


@mcp.tool()
async def nymeria_todo_delete(
    todo_id: str,
    user_id: str = "default",
) -> str:
    """
    Delete a TODO permanently. Cancels any scheduled execution.

    Args:
        todo_id: The 8-character TODO ID to delete
        user_id: User ID (default: "default")

    Returns:
        Confirmation message
    """
    if not todo_id:
        return "Error: todo_id is required."

    agent = _get_agent()

    try:
        from nymeria.core.activity_log import ActivityType, log_activity

        with agent.todo_manager.atomic_update(user_id) as todo_list:
            deleted = todo_list.delete_item(todo_id)
            if deleted:
                logger.info(f"TODO deleted: user={user_id}, id={todo_id}")

                # Remove from schedule database
                if hasattr(agent, '_schedule_db'):
                    agent._schedule_db.remove_scheduled(todo_id)

                # Log activity
                log_activity(
                    ActivityType.TODO_DELETED,
                    f"TODO deleted (MCP): {deleted.task[:80]}",
                    user_id=user_id,
                    metadata={"todo_id": todo_id, "source": "mcp"},
                )

                return f"Deleted: {deleted.task[:100]}"
            else:
                return f"Error: TODO '{todo_id}' not found."
    except Exception as e:
        logger.error(f"nymeria_todo_delete error: {e}", exc_info=True)
        return f"Error: {str(e)}"


# =============================================================================
# Thread History Tool
# =============================================================================

@mcp.tool()
async def nymeria_thread_history(
    thread_id: str,
    limit: int = 20,
) -> str:
    """
    Get conversation history for a thread.

    Returns the message history for a specific conversation thread,
    useful for understanding context or reviewing past interactions.

    Args:
        thread_id: The thread ID to get history for
        limit: Maximum number of messages to return (default: 20)

    Returns:
        Formatted conversation history
    """
    if not thread_id:
        return "Error: thread_id is required."

    agent = _get_agent()

    try:
        history = agent.get_conversation_history(thread_id)

        if not history:
            return f"No history found for thread '{thread_id}'."

        # Limit results
        limit = max(1, min(100, limit))
        history = history[-limit:]

        lines = [f"History for thread '{thread_id}' ({len(history)} message(s)):"]
        lines.append("")

        for msg in history:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")

            # Truncate long content
            if len(content) > 500:
                content = content[:497] + "..."

            lines.append(f"[{role}]: {content}")

            # Include tool calls from steps array (new format) or legacy tool_calls
            steps = msg.get("steps", [])
            tool_calls = [s for s in steps if s.get("type") == "tool_call"] if steps else msg.get("tool_calls", [])
            if tool_calls:
                for tc in tool_calls[:3]:  # Limit tool calls shown
                    lines.append(f"  -> Tool: {tc.get('name', '?')}")

            lines.append("")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"nymeria_thread_history error: {e}", exc_info=True)
        return f"Error: {str(e)}"


# =============================================================================
# Server Entry Points
# =============================================================================

def run_stdio():
    """Run the MCP server in STDIO mode (default)."""
    logger.info("Starting Nymeria MCP server in STDIO mode...")
    mcp.run()


def run_http(host: str = "127.0.0.1", port: int = 8001):
    """Run the MCP server in HTTP mode."""
    logger.info(f"Starting Nymeria MCP server in HTTP mode on {host}:{port}...")
    # Update settings before running (FastMCP.run() doesn't accept host/port)
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    # When run directly, use STDIO mode
    run_stdio()
