"""Self-modification agent for Nymeria."""

import json
import logging
import uuid
from pathlib import Path
from typing import List, Literal, Optional

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool, tool

from ..vendor.react_agent import AgentConfig, CheckpointerConfig, LLMConfig, create_graph

from .backup import BackupManager
from .validator import CodeValidator
from ..config import get_settings

logger = logging.getLogger(__name__)


# Define tools available to the self-modification agent
@tool
def self_file_read(file_path: str) -> str:
    """
    Read a file from the Nymeria codebase.

    Args:
        file_path: Relative path from the project root (e.g., "nymeria/tools/my_tool.py") or absolute path

    Returns:
        File contents or error message
    """
    settings = get_settings()
    project_root = settings.project_root

    # Resolve path
    path = Path(file_path)
    if not path.is_absolute():
        path = project_root / file_path
    path = path.resolve()

    # Security check: must be within project
    try:
        path.relative_to(project_root)
    except ValueError:
        return f"[Error]: Access denied. File must be within {project_root}"

    if not path.exists():
        return f"[Error]: File not found: {file_path}"

    if not path.is_file():
        return f"[Error]: Not a file: {file_path}"

    try:
        content = path.read_text(encoding="utf-8")
        return content
    except Exception as e:
        return f"[Error]: Failed to read file: {e}"


@tool
def self_file_write(file_path: str, content: str) -> str:
    """
    Write content to a file in the tools or agents directory.

    IMPORTANT: Only files in nymeria/tools/ or nymeria/agents/ can be modified.

    Args:
        file_path: Relative path from the project root (e.g., "nymeria/tools/my_tool.py") or absolute path
        content: Content to write

    Returns:
        Success or error message
    """
    settings = get_settings()
    project_root = settings.project_root
    tools_dir = project_root / "nymeria" / "tools"
    agents_dir = project_root / "nymeria" / "agents"
    trigger_sources_dir = project_root / "nymeria" / "triggers" / "sources"

    # Resolve path
    path = Path(file_path)
    if not path.is_absolute():
        path = project_root / file_path
    path = path.resolve()

    # Security check: must be within tools, agents, or trigger sources directory
    in_allowed = False
    for allowed_dir in (tools_dir, agents_dir, trigger_sources_dir):
        try:
            path.relative_to(allowed_dir)
            in_allowed = True
            break
        except ValueError:
            pass

    if not in_allowed:
        return f"[Error]: Access denied. Can only write to files in {tools_dir}, {agents_dir}, or {trigger_sources_dir}"

    # Validate Python syntax
    validator = CodeValidator(project_root)
    valid, msg = validator.validate_python_syntax(content)
    if not valid:
        return f"[Error]: Invalid Python syntax: {msg}"

    # Create backup if file exists
    backup_manager = BackupManager(settings.backups_dir, project_root)
    if path.exists():
        backup_path = backup_manager.create_backup(path)
        if backup_path:
            logger.info(f"Created backup: {backup_path}")

    # Write the file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"[Success]: Wrote {len(content)} characters to {file_path}"
    except Exception as e:
        return f"[Error]: Failed to write file: {e}"


@tool
def self_file_list(directory: str = "nymeria/tools") -> str:
    """
    List files in a directory.

    Args:
        directory: Directory path (default: nymeria/tools)

    Returns:
        List of files
    """
    settings = get_settings()
    project_root = settings.project_root

    path = Path(directory)
    if not path.is_absolute():
        path = project_root / directory
    path = path.resolve()

    # Security check
    try:
        path.relative_to(project_root)
    except ValueError:
        return f"[Error]: Access denied. Directory must be within project"

    if not path.exists():
        return f"[Error]: Directory not found: {directory}"

    if not path.is_dir():
        return f"[Error]: Not a directory: {directory}"

    files = []
    for f in sorted(path.iterdir()):
        if f.is_file():
            files.append(f"  {f.name} ({f.stat().st_size} bytes)")
        else:
            files.append(f"  {f.name}/")

    return f"Contents of {directory}:\n" + "\n".join(files)


@tool
def self_test_import() -> str:
    """
    Test that all tools can be imported successfully.

    Use this after making changes to verify nothing is broken.

    Returns:
        Success or error message
    """
    settings = get_settings()
    validator = CodeValidator(settings.project_root)
    success, msg = validator.test_tool_import()

    if success:
        return f"[Success]: {msg}"
    else:
        return f"[Error]: {msg}"


@tool
def self_file_delete(file_path: str) -> str:
    """
    Delete a file from the tools or agents directory.

    IMPORTANT: Only files in nymeria/tools/ or nymeria/agents/ can be deleted.

    Args:
        file_path: Relative path from the project root (e.g., "nymeria/tools/my_tool.py") or absolute path

    Returns:
        Success or error message
    """
    settings = get_settings()
    project_root = settings.project_root
    tools_dir = project_root / "nymeria" / "tools"
    agents_dir = project_root / "nymeria" / "agents"
    trigger_sources_dir = project_root / "nymeria" / "triggers" / "sources"

    # Resolve path
    path = Path(file_path)
    if not path.is_absolute():
        path = project_root / file_path
    path = path.resolve()

    # Security check: must be within tools, agents, or trigger sources directory
    in_allowed = False
    for allowed_dir in (tools_dir, agents_dir, trigger_sources_dir):
        try:
            path.relative_to(allowed_dir)
            in_allowed = True
            break
        except ValueError:
            pass

    if not in_allowed:
        return f"[Error]: Access denied. Can only delete files in {tools_dir}, {agents_dir}, or {trigger_sources_dir}"

    # Don't allow deleting __init__.py
    if path.name == "__init__.py":
        return "[Error]: Cannot delete __init__.py - this would break the module"

    if not path.exists():
        return f"[Error]: File not found: {file_path}"

    # Create backup before deleting
    backup_manager = BackupManager(settings.backups_dir, project_root)
    backup_path = backup_manager.create_backup(path)
    if backup_path:
        logger.info(f"Created backup before delete: {backup_path}")

    # Delete the file
    try:
        path.unlink()
        return f"[Success]: Deleted {file_path}"
    except Exception as e:
        return f"[Error]: Failed to delete file: {e}"


@tool
def self_reload() -> str:
    """
    Reload all tools and agents after making changes.

    Call this after using self_file_write to create or modify tool/agent files
    and updating __init__.py. This makes newly created tools live in Nymeria's
    registry so they can be tested with self_invoke_tool.

    Returns:
        Updated tool list or error message
    """
    from .agent import get_current_agent

    try:
        agent = get_current_agent()
        if agent is None:
            return "[Error]: No active agent found. Tools cannot be reloaded."

        tool_names = agent.reload_tools()
        return f"[Success]: Reloaded. {len(tool_names)} tools available: {', '.join(tool_names)}"
    except Exception as e:
        logger.error(f"self_reload failed: {e}", exc_info=True)
        return f"[Error]: Reload failed: {str(e)}"


@tool
def self_invoke_tool(tool_name: str, arguments_json: str) -> str:
    """
    Test a tool by invoking it with the given arguments.

    Use this after self_reload() to verify a newly created tool works correctly.
    If the tool fails, fix the code and repeat the create→reload→test cycle.

    Args:
        tool_name: Name of the tool to test (e.g., "my_new_tool")
        arguments_json: JSON string of arguments to pass (e.g., '{"param": "value"}')

    Returns:
        The tool's output or an error message
    """
    from .agent import get_current_agent

    try:
        agent = get_current_agent()
        if agent is None:
            return "[Error]: No active agent found. Cannot invoke tool."

        # Find tool in registry
        tool_obj = agent.tool_registry.get_tool(tool_name)
        if not tool_obj:
            available = [t["name"] for t in agent.tool_registry.list_tools()]
            return f"[Error]: Tool '{tool_name}' not found. Available tools: {', '.join(available[:20])}"

        # Parse arguments
        try:
            args = json.loads(arguments_json)
        except json.JSONDecodeError as e:
            return f"[Error]: Invalid JSON arguments: {e}"

        # Invoke the tool
        result = tool_obj.invoke(args)
        return f"[Test result]: {result}"

    except Exception as e:
        logger.error(f"self_invoke_tool failed: {e}", exc_info=True)
        return f"[Error]: Tool invocation failed: {str(e)}"


# Tools available to the self-modification agent
SELF_AGENT_TOOLS: List[BaseTool] = [
    self_file_read,
    self_file_write,
    self_file_list,
    self_file_delete,
    self_test_import,
    self_reload,
    self_invoke_tool,
]


class SelfModifyAgent:
    """
    Sub-agent for self-modification tasks.

    This agent is invoked by the main NymeriaAgent when self-modification
    is needed (adding tools, fixing bugs, etc.).
    """

    ALLOWED_CATEGORIES = {"add_tool", "remove_tool", "fix_bug", "explain", "add_agent", "modify_agent", "remove_agent", "add_trigger_source", "remove_trigger_source"}

    def __init__(self):
        """Initialize the self-modification agent."""
        self.settings = get_settings()
        self.project_root = self.settings.project_root
        self.backup_manager = BackupManager(self.settings.backups_dir, self.project_root)
        self.validator = CodeValidator(self.project_root)

        # Load specialized system prompt
        self.system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        """Load the self-agent system prompt."""
        prompt_path = self.project_root / "nymeria" / "config" / "self_agent_prompt.md"
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return "You are a code modification agent. Help modify Nymeria's tools."

    def _get_agent_config(self) -> dict:
        """Get agent config from registry, with fallback defaults."""
        from ..agents import get_agent

        config = get_agent("SelfModifyAgent")
        if config:
            return config

        # Fallback defaults if not registered
        return {
            "llm_provider": "openrouter",
            "llm_model": "anthropic/claude-opus-4.5",
            "llm_temperature": 0.5,
            "llm_max_tokens": 16000,
        }

    def _get_api_key_for_provider(self, provider: str) -> str:
        """Get API key for a specific provider."""
        if provider == "anthropic":
            return self.settings.anthropic_api_key
        elif provider == "openai":
            return self.settings.openai_api_key
        elif provider == "openrouter":
            return self.settings.openrouter_api_key
        else:
            return self.settings.get_api_key_for_provider()

    def _create_graph(self):
        """Create a LangGraph for the self-modification agent."""
        # Get LLM config from registered agent (allows UI configuration)
        agent_config = self._get_agent_config()

        provider = agent_config.get("llm_provider", "openrouter")
        model = agent_config.get("llm_model", "anthropic/claude-opus-4.5")
        temperature = agent_config.get("llm_temperature", 0.5)
        max_tokens = agent_config.get("llm_max_tokens", 16000)  # Limit to avoid credit errors

        config = AgentConfig(
            llm=LLMConfig(
                provider=provider,
                model=model,
                api_key=self._get_api_key_for_provider(provider),
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            checkpointer=CheckpointerConfig(backend="memory"),
            system_prompt=self.system_prompt,
            max_iterations=30,  # Allow multiple steps for complex modifications
            verbose=self.settings.log_level == "DEBUG",
        )

        return create_graph(config=config, tools=SELF_AGENT_TOOLS)

    def execute(
        self,
        instruction: str,
        category: Literal["add_tool", "remove_tool", "fix_bug", "explain", "add_agent", "modify_agent", "remove_agent", "add_trigger_source", "remove_trigger_source"],
    ) -> str:
        """
        Execute a self-modification task.

        Args:
            instruction: Description of what to do
            category: Type of modification

        Returns:
            Result message
        """
        if category not in self.ALLOWED_CATEGORIES:
            return f"[Error]: Unknown category '{category}'. Allowed: {self.ALLOWED_CATEGORIES}"

        logger.info(f"SelfModifyAgent executing: category={category}, instruction={instruction[:100]}...")

        # Build the task message
        if category == "add_tool":
            task = f"""## Task: Add New Tool

{instruction}

## Instructions
1. First, read nymeria/tools/__init__.py to see current tools
2. Read an existing tool file (like filesystem.py) to understand the pattern
3. Create the new tool following the template in your system prompt
4. Update __init__.py to export the new tool
5. Run self_test_import() to verify everything works
6. Report what you did
"""
        elif category == "remove_tool":
            task = f"""## Task: Remove Tool

{instruction}

## Instructions
1. First, read nymeria/tools/__init__.py to identify the tool to remove
2. Delete the tool's .py file (if it has its own file)
3. Update __init__.py to remove ALL references to the tool:
   - Remove the import statement
   - Remove from ALL_TOOLS list
   - Remove from __all__ list
4. Run self_test_import() to verify everything still works
5. Report what you removed
"""
        elif category == "fix_bug":
            task = f"""## Task: Fix Bug

{instruction}

## Instructions
1. Read the relevant file(s) to understand the issue
2. Make the necessary fix
3. Run self_test_import() to verify the fix
4. Report what you changed
"""
        elif category == "explain":
            task = f"""## Task: Explain Code

{instruction}

## Instructions
1. Read the relevant file(s)
2. Explain how the code works
3. Do NOT modify any files
"""
        elif category == "add_agent":
            # Extract a snake_case name from the instruction for file naming
            task = f"""## Task: Create Sub-Agent

{instruction}

## Instructions
1. Read nymeria/agents/__init__.py to understand the registration system
2. Read an existing agent file in nymeria/agents/ (like browser_agent.py) to understand the pattern
3. Determine a good snake_case name for the agent file (e.g., outlook_agent)
4. Create the agent file: nymeria/agents/{{agent_name}}.py with:
   - Import tools from existing tool files (e.g., from ..tools.outlook_auth import ...)
   - A system prompt describing the agent's purpose and available tools
   - A config dict with these fields:
     * name: Display name (e.g., "OutlookAgent")
     * description: Short description for tool discovery
     * system_prompt: The agent's system prompt
     * tools: List of tool functions to pass directly to the agent
     * context_turns: Number of conversation turns to remember (3-10)
     * allowed_tools: List of additional tool names from ALL_TOOLS (usually empty)
     * required_env_vars: List of required env var names (e.g., ["OPENROUTER_API_KEY"])
     * llm_provider: (optional) "anthropic", "openai", or "openrouter" - defaults to global setting
     * llm_model: (optional) Model name like "x-ai/grok-4.1-fast" - defaults to global setting
     * llm_temperature: (optional) Temperature 0.0-2.0 - defaults to 0.0
   - A call to register_agent() at the end
5. If the user specifies a model (e.g., "use grok-4.1-fast"), include llm_provider/llm_model/llm_temperature
6. Run self_test_import() to verify everything imports correctly
7. Report:
   - What agent was created (name and description)
   - What tools it has
   - What LLM model it uses (if custom)
   - What environment variables are required
   - How to invoke it: AgentName(task="...") or sub_agent('AgentName', 'instruction')

NOTE: If the agent uses existing tools from nymeria/tools/, import them directly rather than creating new tool files.
"""
        elif category == "modify_agent":
            task = f"""## Task: Modify Sub-Agent

{instruction}

## Instructions
1. Read nymeria/agents/__init__.py to find the agent
2. Read the agent's files (agent file and tools file)
3. Make the requested changes
4. Run self_test_import() to verify the changes work
5. Report what you changed
"""
        elif category == "remove_agent":
            task = f"""## Task: Remove Sub-Agent

{instruction}

## Instructions
1. Read nymeria/agents/__init__.py to find the agent to remove
2. Delete the agent file: nymeria/agents/{{agent_name}}.py
3. Delete the tools file: nymeria/agents/{{agent_name}}_tools.py
4. Run self_test_import() to verify everything still works
5. Report what was removed
"""
        elif category == "add_trigger_source":
            task = f"""## Task: Create Trigger Source Plugin

{instruction}

## Instructions
1. Read nymeria/triggers/sources/__init__.py to understand the registry system
2. Read nymeria/triggers/sources/base.py for the BaseTriggerSource contract
3. Read nymeria/triggers/sources/webhook_source.py as an example
4. Create: nymeria/triggers/sources/{{source_name}}_source.py with:
   - A class extending BaseTriggerSource
   - name, description, and config_schema class attributes
   - A check(config, state) method that returns list of event dicts
   - check() must be LIGHTWEIGHT -- no LLM calls!
   - Use state dict to persist cursors/timestamps between polls
   - Call register_source() at module level
5. Run self_test_import() to verify
6. Report what source was created and how to use it
"""
        elif category == "remove_trigger_source":
            task = f"""## Task: Remove Trigger Source Plugin

{instruction}

## Instructions
1. List files in nymeria/triggers/sources/ to find the source
2. Delete the source file
3. Run self_test_import() to verify
4. Report what was removed
"""
        else:
            return f"[Error]: Unknown category '{category}'. Allowed: {self.ALLOWED_CATEGORIES}"

        # Create and run the graph
        try:
            graph = self._create_graph()
            thread_id = f"self-modify-{uuid.uuid4().hex[:8]}"

            # Isolate from parent's streaming context to prevent tool call leakage
            # These context vars capture messages during streaming and propagate to nested invocations
            from langchain_core.runnables.config import var_child_runnable_config
            from langchain_core.callbacks.manager import tracing_v2_callback_var
            from langchain_core.tracers.context import run_collector_var

            # Save current values and reset
            saved_config = var_child_runnable_config.get(None)
            saved_callback = tracing_v2_callback_var.get(None)
            saved_collector = run_collector_var.get(None)

            config_token = var_child_runnable_config.set(None)
            callback_token = tracing_v2_callback_var.set(None)
            collector_token = run_collector_var.set(None)

            try:
                result = graph.invoke(
                    {"messages": [HumanMessage(content=task)]},
                    config={
                        "recursion_limit": 70,
                        "configurable": {"thread_id": thread_id},
                    },
                )
            finally:
                # Restore context
                var_child_runnable_config.reset(config_token)
                tracing_v2_callback_var.reset(callback_token)
                run_collector_var.reset(collector_token)

            # Extract the final response
            messages = result.get("messages", [])
            for msg in reversed(messages):
                if isinstance(msg, AIMessage) and msg.content:
                    return msg.content

            return "[Error]: No response generated from self-modification agent"

        except Exception as e:
            logger.error(f"SelfModifyAgent error: {e}", exc_info=True)
            return f"[Error]: Self-modification failed: {str(e)}"

    def rollback_last(self, file_path: str) -> str:
        """
        Rollback a file to its most recent backup.

        Args:
            file_path: Path to the file to rollback

        Returns:
            Success or error message
        """
        path = Path(file_path)
        if not path.is_absolute():
            path = self.project_root / file_path

        success = self.backup_manager.restore_backup(path)
        if success:
            return f"[Success]: Rolled back {file_path} to previous version"
        else:
            return f"[Error]: No backup found for {file_path}"
