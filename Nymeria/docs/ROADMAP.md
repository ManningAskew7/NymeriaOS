# Nymeria Roadmap

This document outlines planned features for Nymeria that require more complex implementation.

---

## Tool Management UI

### Overview
Allow power users to create, edit, and manage custom tools through the desktop UI rather than editing Python files directly.

### Current State
- Tools are defined in Python files under `nymeria/tools/`
- Each tool is a decorated function using `@tool` from LangChain
- Tools are registered in `nymeria/tools/__init__.py`
- Adding new tools requires Python knowledge and server restart

### Proposed Architecture

#### 1. Tool Definition Format
Create a JSON/YAML schema for tool definitions:

```yaml
name: search_web
description: Search the web for current information
parameters:
  query:
    type: string
    description: The search query
    required: true
  num_results:
    type: integer
    default: 5
    min: 1
    max: 20
implementation:
  type: http  # http, python_snippet, mcp
  config:
    method: GET
    url: "https://api.example.com/search"
    headers:
      Authorization: "Bearer ${SEARCH_API_KEY}"
    params:
      q: "${query}"
      limit: "${num_results}"
```

#### 2. Implementation Types

| Type | Description | Use Case |
|------|-------------|----------|
| `http` | Make HTTP API calls | REST APIs, webhooks |
| `python_snippet` | Execute sandboxed Python | Simple transformations |
| `mcp` | Model Context Protocol | Connect to MCP servers |
| `composite` | Chain multiple tools | Complex workflows |

#### 3. Backend Changes

**New files:**
- `nymeria/core/custom_tools.py` - Custom tool loader and executor
- `nymeria/tools/definitions/` - Directory for user tool YAML files

**API endpoints:**
```
GET    /tools/custom           - List user-defined tools
POST   /tools/custom           - Create new tool
PUT    /tools/custom/{id}      - Update tool definition
DELETE /tools/custom/{id}      - Remove tool
POST   /tools/custom/{id}/test - Test tool execution
```

#### 4. Frontend UI

**Tool Editor Modal:**
- Name and description fields
- Parameter builder (add/remove params, set types/defaults)
- Implementation type selector
- Configuration editor (depends on type)
- Test panel with sample input/output
- Enable/disable toggle

**Tool Library View:**
- List of all tools (built-in + custom)
- Built-in tools shown as read-only
- Custom tools with edit/delete actions
- Import/export functionality

#### 5. Security Considerations

- **Sandboxing**: Python snippets run in restricted environment
- **Secret management**: API keys stored securely, referenced by name
- **Rate limiting**: Prevent abuse of HTTP tools
- **Validation**: Schema validation for all tool definitions
- **Audit logging**: Track tool creation/modification/usage

#### 6. Migration Path

1. Start with HTTP-only tools (safest)
2. Add MCP support for extensibility
3. Python snippets last (requires sandboxing)

---

## Sub-Agent System

### Overview
Allow users to create specialized sub-agents with custom prompts, tool subsets, and behaviors that the main agent can delegate to.

### Current State
- Single agent with full system prompt (`soul.md`)
- All tools available to the agent
- No delegation or specialization

### Proposed Architecture

#### 1. Sub-Agent Definition

```yaml
name: research_assistant
description: Specialized in gathering and synthesizing information
system_prompt: |
  You are a research assistant focused on finding accurate information.
  Always cite your sources and verify claims from multiple sources.
  Prefer recent information when possible.
tools:
  - search_web
  - read_url
  - summarize_text
constraints:
  max_iterations: 5
  timeout_seconds: 120
trigger:
  keywords: ["research", "find out", "look up"]
  # or explicit: only when main agent calls delegate_to("research_assistant")
```

#### 2. Delegation Mechanism

**Option A: Explicit Delegation (Recommended)**
Main agent gets a `delegate_to` tool:
```python
@tool
def delegate_to(agent_name: str, task: str) -> str:
    """Delegate a task to a specialized sub-agent."""
```

**Option B: Automatic Routing**
System detects when to route based on:
- Keyword matching in user message
- Intent classification
- Tool requirements

#### 3. Backend Implementation

**New files:**
- `nymeria/core/sub_agents.py` - Sub-agent loader and executor
- `nymeria/agents/definitions/` - Sub-agent YAML files

**Key classes:**
```python
class SubAgentConfig:
    name: str
    description: str
    system_prompt: str
    allowed_tools: List[str]
    max_iterations: int
    timeout: int

class SubAgentExecutor:
    def __init__(self, config: SubAgentConfig)
    async def run(self, task: str, context: Dict) -> str
```

#### 4. Context Sharing

Sub-agents need access to:
- User profile (preferences, history)
- Current thread context (recent messages)
- Shared memory (facts, preferences)

But isolated from:
- Full conversation history
- Other sub-agent conversations
- Admin/system tools

#### 5. Frontend UI

**Sub-Agent Manager:**
- List of defined sub-agents
- Create/edit modal:
  - Name and description
  - System prompt editor (with variables)
  - Tool picker (checkbox list)
  - Constraints configuration
  - Trigger settings
- Test panel (send task, see response)
- Usage statistics

**Chat Integration:**
- Visual indicator when sub-agent is active
- Different bubble style for sub-agent responses
- Expandable "delegation chain" view

#### 6. Example Sub-Agents

| Agent | Purpose | Tools |
|-------|---------|-------|
| `researcher` | Deep information gathering | search, read_url, summarize |
| `coder` | Code generation and review | read_file, write_file, run_code |
| `planner` | Task breakdown and scheduling | todo_*, schedule_* |
| `analyst` | Data analysis and visualization | python_exec, create_chart |

#### 7. Technical Considerations

- **Token budget**: Sub-agents have smaller context windows
- **Loop prevention**: Limit delegation depth (no agent calling itself)
- **State management**: Clear handoff of relevant state
- **Error handling**: Graceful degradation if sub-agent fails
- **Observability**: Log delegation chains for debugging

---

## Implementation Priority

### Phase 1: Foundation
1. Tool definition schema and validation
2. HTTP tool implementation
3. Basic tool management API

### Phase 2: Tool UI
1. Tool list view in desktop
2. Create/edit modal for HTTP tools
3. Tool testing interface

### Phase 3: Sub-Agents Foundation
1. Sub-agent configuration schema
2. `delegate_to` tool
3. Basic sub-agent executor

### Phase 4: Sub-Agent UI
1. Sub-agent manager view
2. System prompt editor
3. Tool picker and constraints

### Phase 5: Advanced Features
1. MCP tool support
2. Python snippet tools (sandboxed)
3. Composite tools/workflows
4. Automatic routing for sub-agents

---

## Open Questions

1. **Tool versioning**: How to handle changes to tool definitions?
2. **Sharing**: Should users be able to share tools/agents?
3. **Marketplace**: Central repository of community tools?
4. **Testing**: Automated testing for custom tools?
5. **Permissions**: Fine-grained access control for tools?

---

## Related Documents

- [Architecture Overview](./architecture.md)
- [Tools Reference](./tools.md)
- [API Documentation](./api.md)
