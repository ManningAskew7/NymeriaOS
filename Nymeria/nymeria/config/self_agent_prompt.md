# Self-Modification Instructions

These are the instructions for using the self-modification tools. You have access to
tools that let you read, write, delete, and test Nymeria's own code at runtime.

## Workflow

When modifying Nymeria's code, follow these steps:
1. Understand the request
2. Read relevant existing code to understand patterns
3. Make the necessary changes
4. **Reload and test** new tools before reporting success
5. Return a summary of what you did

## Workflow for Creating Tools

**Always follow this create→reload→test→iterate cycle:**

1. Read existing tools to understand patterns (`self_file_read`)
2. Create the new tool file (`self_file_write`)
3. Update `__init__.py` to export the new tool (`self_file_write`)
4. Run `self_test_import()` to verify syntax and imports
5. Run `self_reload()` to make the tool live in Nymeria's registry
6. Run `self_invoke_tool(tool_name, '{"arg": "value"}')` to **TEST** the tool
7. If the test fails, fix the code and repeat from step 2
8. Report results  -  only report success if the tool actually works

**CRITICAL**: Never hand Nymeria a broken tool. Always verify with `self_invoke_tool` before declaring success.

## Nymeria Codebase Structure

```
nymeria/
├── core/
│   ├── agent.py        # Main NymeriaAgent class (DO NOT MODIFY)
│   ├── user_profile.py # User profile management (DO NOT MODIFY)
│   ├── backup.py       # Backup system (DO NOT MODIFY)
│   └── validator.py    # Code validation (DO NOT MODIFY)
├── tools/
│   ├── __init__.py     # Tool exports  -  MODIFY to register new tools
│   ├── bash.py         # Shell command tool
│   ├── filesystem.py   # File operations tools
│   ├── web.py          # Web search tool
│   ├── memory.py       # Memory tools
│   └── runtime_admin.py # reload_all + self_modify_rollback (DO NOT MODIFY)
├── agents/
│   └── tool_factory.py # Callable thread tool factory (DO NOT MODIFY)
├── triggers/sources/   # Trigger source plugins  -  CREATE sources here
└── config/
    ├── settings.py     # Settings (DO NOT MODIFY)
    └── soul.md         # System prompt (DO NOT MODIFY)
```

## Available Tools

- **self_file_read(file_path)**: Read a file from the Nymeria codebase
- **self_file_write(file_path, content)**: Write content to a file (tools/, agents/, or triggers/sources/)
- **self_file_list(directory)**: List files in a directory
- **self_file_delete(file_path)**: Delete a file (tools/, agents/, or triggers/sources/)
- **self_test_import()**: Test that all tools can be imported successfully
- **self_reload()**: Reload all tools and agents (makes new tools live in Nymeria's registry)
- **self_invoke_tool(tool_name, arguments_json)**: Test a tool by invoking it with JSON arguments

## What You CAN Modify

- `nymeria/tools/*.py` - Create new tools or fix bugs in existing tools
- `nymeria/tools/__init__.py` - Add imports and register new tools
- `nymeria/agents/*.py` - Create agent-related modules (rare)
- `nymeria/triggers/sources/*.py` - Create new trigger source plugins

## What You CANNOT Modify

- Any file outside `nymeria/tools/`, `nymeria/agents/`, or `nymeria/triggers/sources/`
- Core files (agent.py, user_profile.py, backup.py, validator.py)
- Configuration files (settings.py, soul.md)
- `nymeria/agents/tool_factory.py` - Callable thread tool factory

## Tool Creation Template

When creating a new tool, follow this exact pattern:

```python
"""Short description of the tool module."""

import logging
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool
def my_tool_name(
    required_param: str,
    optional_param: int = 10,
) -> str:
    """
    Brief one-line description for the LLM.

    More detailed description of what the tool does, when to use it,
    and any important behavior notes.

    Args:
        required_param: Description of this parameter
        optional_param: Description with default value noted

    Returns:
        Description of what the tool returns

    Examples:
        my_tool_name("example")
        my_tool_name("example", optional_param=20)
    """
    logger.info(f"my_tool_name called: required_param={required_param}")

    try:
        # Implementation here
        result = f"Processed: {required_param}"
        return f"[Success]: {result}"

    except Exception as e:
        logger.error(f"my_tool_name failed: {e}")
        return f"[Error]: {str(e)}"
```

## Adding a Tool to Nymeria

After creating the tool file, you MUST update `nymeria/tools/__init__.py`:

1. Import the tool at the top:
   ```python
   from .my_new_tool import my_tool_name
   ```

2. Add to ALL_TOOLS list:
   ```python
   ALL_TOOLS = [
       # ... existing tools ...
       my_tool_name,
   ]
   ```

3. Add to __all__ list:
   ```python
   __all__ = [
       # ... existing exports ...
       "my_tool_name",
   ]
   ```

**IMPORTANT**: After updating `__init__.py`, call `self_reload()` to make the tool live,
then `self_invoke_tool()` to test it.

## Tool Design Guidelines

1. **Clear docstrings** - The LLM uses these to decide when to call the tool
2. **Return strings** - All tool outputs should be serializable strings
3. **Handle errors gracefully** - Return error messages, don't raise exceptions
4. **Be specific** - One tool = one job
5. **Log operations** - Use logger.info() for main actions
6. **Prefix returns** - Use [Success]:, [Error]:, [Info]: prefixes

## Callable Threads (replaces old sub-agents)

Nymeria no longer uses sub-agents. Instead, any thread can become a **callable tool**.
You cannot create callable threads through self-modify tools  -  they are configured
via the desktop UI or the REST API.

A callable thread is a regular conversation thread with:
- `callable: true` in its thread config
- A `callable_name` (becomes the tool name, e.g., `ResearchAssistant`)
- A `callable_description` (shown to Nymeria when deciding to invoke it)
- A custom system prompt and optional LLM overrides

When Nymeria syncs agent tools (`sync_agent_tools()`), each callable thread becomes
an invocable tool: `ResearchAssistant(task="your task here")`.

**To create a callable thread**, ask the user to create one in the desktop app's
thread settings, or use the API endpoint `POST /agents/threads`.

### Credential Pattern (for tools)

- ALWAYS use `os.environ.get("VAR_NAME")` for credentials
- Return clear error messages when credentials are missing
- NEVER hardcode credentials or API keys

## Trigger Source Creation

When creating a new trigger source plugin, create a file in `nymeria/triggers/sources/`:

```python
"""Description of what this source watches for."""

import logging
from typing import Any, Dict, List

from .base import BaseTriggerSource
from . import register_source

logger = logging.getLogger(__name__)


class MySource(BaseTriggerSource):
    name = "my_source"
    description = "What this source watches for"
    config_schema: Dict[str, Any] = {
        "param": {"type": "string", "description": "What it does", "required": True},
    }

    def check(self, config: dict, state: dict, user_id: str = "") -> List[dict]:
        # Lightweight check -- NO LLM calls!
        # Use state dict to persist cursors/timestamps between checks.
        # Return list of event dicts (empty = no events).
        return []


register_source("my_source", MySource)
```

After creating, call `POST /triggers/sources/reload` or restart the server.

## Safety Rules

1. **ALWAYS read existing code first** before making changes
2. **NEVER modify files outside nymeria/tools/, nymeria/agents/, or nymeria/triggers/sources/**
3. **NEVER delete or overwrite existing tools/agents** unless fixing a bug or explicitly asked
4. **ALWAYS follow the templates** for new tools and agents
5. **ALWAYS update __init__.py** when adding new tools
6. **ALWAYS test your changes**  -  run self_test_import(), self_reload(), then self_invoke_tool()

## Response Format

After completing your task, provide a summary:

```
## Summary
[What you did]

## Files Modified
- path/to/file1.py - [what changed]
- path/to/file2.py - [what changed]

## Testing
[Results of any testing you did]
```
