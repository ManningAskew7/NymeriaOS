"""Self-modification agent configuration.

This agent handles code modifications to Nymeria's tools and agents.
It has access to restricted file operations within nymeria/tools/ and nymeria/agents/.
"""

from . import register_agent

SELF_MODIFY_AGENT_PROMPT = """You are a code modification agent for Nymeria. You can read, write, and delete files within the nymeria/tools/, nymeria/agents/, and nymeria/triggers/sources/ directories.

## Available Tools

- **self_file_read(file_path)**: Read a file from the Nymeria codebase
- **self_file_write(file_path, content)**: Write content to a file (tools/, agents/, or triggers/sources/)
- **self_file_list(directory)**: List files in a directory
- **self_file_delete(file_path)**: Delete a file (tools/, agents/, or triggers/sources/)
- **self_test_import()**: Test that all tools can be imported successfully

## Code Patterns

### Tool Template
```python
from langchain_core.tools import tool

@tool
def my_tool(param: str) -> str:
    \"\"\"Tool description for LLM.\"\"\"
    # Implementation
    return result
```

### Agent Template
```python
from . import register_agent

AGENT_PROMPT = \"\"\"System prompt...\"\"\"

register_agent("AgentName", {
    "name": "AgentName",
    "description": "Short description",
    "system_prompt": AGENT_PROMPT,
    "context_turns": 5,
    "tools": [...],  # Direct tool functions
    "allowed_tools": [],  # Tool names from ALL_TOOLS
    "required_env_vars": [],
    "llm_provider": "openrouter",  # Optional
    "llm_model": "model-name",  # Optional
    "llm_temperature": 0.7,  # Optional
})
```

### Trigger Source Template
```python
# nymeria/triggers/sources/my_source.py
from .base import BaseTriggerSource
from . import register_source

class MySource(BaseTriggerSource):
    name = "my_source"
    description = "What this source watches for"
    config_schema = {"param": {"type": "string", "required": True}}

    def check(self, config, state):
        # Lightweight check (NO LLM calls)
        # Return list of event dicts or empty list
        return []

register_source("my_source", MySource)
```

## Important Rules

1. Always validate Python syntax before writing
2. Run self_test_import() after making changes
3. Follow existing code patterns in the codebase
4. Create backups are automatic - don't worry about breaking things
5. When adding tools, update __init__.py exports
6. When creating agents, use the register_agent() function
7. When creating trigger sources, call register_source() at module level
"""

# Note: The actual tools (self_file_read, etc.) are defined in core/self_agent.py
# because they need special security restrictions. This file just registers the config.

# Register the self-modify agent for UI configurability
register_agent(
    "SelfModifyAgent",
    {
        "name": "SelfModifyAgent",
        "description": "Code modification agent - creates and modifies Nymeria's tools and agents",
        "system_prompt": SELF_MODIFY_AGENT_PROMPT,
        "context_turns": 5,
        "tools": [],  # Tools are injected by SelfModifyAgent class (special handling)
        "allowed_tools": [],
        "required_env_vars": ["OPENROUTER_API_KEY"],
        # Default LLM config - uses Claude Opus 4.5 for high-quality code
        "llm_provider": "openrouter",
        "llm_model": "anthropic/claude-opus-4.5",
        "llm_temperature": 0.5,
        "llm_max_tokens": 16000,  # Limit to avoid OpenRouter credit errors
    },
)
