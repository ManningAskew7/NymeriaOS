"""Runtime executor for sub-agents."""

import concurrent.futures
import json
import logging
import os
from pathlib import Path
from typing import List, Optional

from langchain_core.messages import AIMessage, HumanMessage

from ..vendor.react_agent import AgentConfig, CheckpointerConfig, LLMConfig, create_graph

from ..config import get_settings

logger = logging.getLogger(__name__)


class SubAgentExecutor:
    """Executes sub-agents with isolated context."""

    def __init__(self):
        self.settings = get_settings()
        self.contexts_dir = self.settings.data_dir / "subagent_contexts"
        self.contexts_dir.mkdir(parents=True, exist_ok=True)

    def invoke(self, agent_name: str, instruction: str, user_id: str = "default") -> str:
        """
        Invoke a sub-agent.

        Args:
            agent_name: Name of the agent to invoke
            instruction: What to tell the agent
            user_id: User ID for context isolation

        Returns:
            Agent's response
        """
        from ..agents import get_agent

        agent_config = get_agent(agent_name)
        if not agent_config:
            available = self._list_available_agents()
            if available:
                return f"[Error]: Agent '{agent_name}' not found. Available agents: {available}"
            return f"[Error]: Agent '{agent_name}' not found. No agents available. Create one with self_modify."

        # Check required env vars
        missing = self._check_env_vars(agent_config.get("required_env_vars", []))
        if missing:
            return f"[Error]: Missing environment variables: {', '.join(missing)}. Set these before using {agent_name}."

        # Load context
        context = self._load_context(agent_name, user_id)

        # Build graph
        graph = self._create_graph(agent_config)

        # Prepare messages (context + new instruction)
        messages = context + [HumanMessage(content=instruction)]

        # Execute with context isolation
        result = self._execute_isolated(graph, messages, agent_name)

        # Save updated context (trimmed)
        self._save_context(
            agent_name,
            user_id,
            messages + [AIMessage(content=result)],
            max_turns=agent_config.get("context_turns", 5)
        )

        return result

    def _list_available_agents(self) -> str:
        """Get comma-separated list of available agents."""
        from ..agents import AVAILABLE_AGENTS
        if AVAILABLE_AGENTS:
            return ", ".join(AVAILABLE_AGENTS.keys())
        return ""

    def _check_env_vars(self, required: List[str]) -> List[str]:
        """Return list of missing env vars."""
        return [var for var in required if not os.environ.get(var)]

    def _load_context(self, agent_name: str, user_id: str) -> List:
        """Load previous context for this agent+user."""
        context_file = self.contexts_dir / f"{user_id}_{agent_name}.json"
        if context_file.exists():
            try:
                data = json.loads(context_file.read_text(encoding="utf-8"))
                messages = []
                for msg in data.get("messages", []):
                    if msg["role"] == "human":
                        messages.append(HumanMessage(content=msg["content"]))
                    else:
                        messages.append(AIMessage(content=msg["content"]))
                return messages
            except Exception as e:
                logger.warning(f"Failed to load context for {agent_name}: {e}")
                return []
        return []

    def _save_context(self, agent_name: str, user_id: str, messages: List, max_turns: int):
        """Save context, trimmed to max_turns."""
        # Keep last N turns (1 turn = human + ai message pair)
        trimmed = messages[-(max_turns * 2):]

        context_file = self.contexts_dir / f"{user_id}_{agent_name}.json"
        data = {
            "messages": [
                {"role": "human" if isinstance(m, HumanMessage) else "ai", "content": m.content}
                for m in trimmed
            ]
        }
        try:
            context_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to save context for {agent_name}: {e}")

    def _get_api_key_for_provider(self, provider: str) -> str:
        """Get API key for a specific provider."""
        if provider == "anthropic":
            return self.settings.anthropic_api_key
        elif provider == "openai":
            return self.settings.openai_api_key
        elif provider == "openrouter":
            return self.settings.openrouter_api_key
        else:
            # Default to the global settings method
            return self.settings.get_api_key_for_provider()

    def _create_graph(self, agent_config: dict):
        """Create LangGraph for sub-agent."""
        # Get tools - agent's own tools + any allowed additional tools
        tools = list(agent_config.get("tools", []))

        # Add any allowed global tools
        allowed = agent_config.get("allowed_tools", [])
        if allowed:
            from ..tools import ALL_TOOLS
            for t in ALL_TOOLS:
                if t.name in allowed:
                    tools.append(t)

        # Use per-agent LLM config if specified, otherwise fall back to global settings
        provider = agent_config.get("llm_provider") or self.settings.llm_provider
        model = agent_config.get("llm_model") or self.settings.llm_model
        temperature = agent_config.get("llm_temperature") if agent_config.get("llm_temperature") is not None else 0.0
        max_tokens = agent_config.get("llm_max_tokens") or 16000  # Default limit to avoid credit errors
        api_key = self._get_api_key_for_provider(provider)

        tool_timeout = self.settings.tool_timeout if hasattr(self.settings, "tool_timeout") else 300

        config = AgentConfig(
            llm=LLMConfig(
                provider=provider,
                model=model,
                api_key=api_key,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt=agent_config["system_prompt"],
            max_iterations=30,
            tool_timeout=tool_timeout,
            verbose=self.settings.log_level == "DEBUG",
        )

        return create_graph(config=config, tools=tools)

    def _execute_isolated(self, graph, messages: List, agent_name: str) -> str:
        """Execute graph with context isolation and a timeout.

        The entire sub-agent execution is bounded by tool_timeout (default 300s / 5 min)
        to prevent a hanging sub-agent from blocking the parent agent indefinitely.
        """
        import uuid
        from langchain_core.runnables.config import var_child_runnable_config
        from langchain_core.callbacks.manager import tracing_v2_callback_var
        from langchain_core.tracers.context import run_collector_var

        timeout = self.settings.tool_timeout if hasattr(self.settings, "tool_timeout") else 300

        # Save and reset context vars to isolate from parent agent
        config_token = var_child_runnable_config.set(None)
        callback_token = tracing_v2_callback_var.set(None)
        collector_token = run_collector_var.set(None)

        def _run_graph():
            thread_id = f"subagent-{agent_name}-{uuid.uuid4().hex[:8]}"
            return graph.invoke(
                {"messages": messages},
                config={
                    "recursion_limit": 70,
                    "configurable": {"thread_id": thread_id},
                },
            )

        try:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = executor.submit(_run_graph)
            try:
                result = future.result(timeout=timeout)
                executor.shutdown(wait=False)
            except concurrent.futures.TimeoutError:
                # shutdown(wait=False) returns immediately — the daemon worker
                # thread will finish on its own (or when the process exits).
                executor.shutdown(wait=False)
                logger.error(
                    f"Sub-agent '{agent_name}' timed out after {timeout}s. "
                    f"The agent will continue but the sub-agent may still be running in the background."
                )
                return (
                    f"[Error]: {agent_name} timed out after {timeout} seconds. "
                    f"The sub-agent took too long and was stopped. "
                    f"Report this timeout to the user — do NOT retry."
                )

            # Extract response
            for msg in reversed(result.get("messages", [])):
                if isinstance(msg, AIMessage) and msg.content:
                    return msg.content

            return "[Error]: No response from sub-agent"

        except Exception as e:
            logger.error(f"Sub-agent execution failed: {e}", exc_info=True)
            return f"[Error]: Sub-agent execution failed: {str(e)}"

        finally:
            var_child_runnable_config.reset(config_token)
            tracing_v2_callback_var.reset(callback_token)
            run_collector_var.reset(collector_token)

    def clear_context(self, agent_name: str, user_id: str = "default") -> bool:
        """
        Clear saved context for an agent.

        Args:
            agent_name: Name of the agent
            user_id: User ID

        Returns:
            True if context was cleared, False if not found
        """
        context_file = self.contexts_dir / f"{user_id}_{agent_name}.json"
        if context_file.exists():
            context_file.unlink()
            logger.info(f"Cleared context for {agent_name} (user: {user_id})")
            return True
        return False
