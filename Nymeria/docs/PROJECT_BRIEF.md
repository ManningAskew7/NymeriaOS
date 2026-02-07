# Nymeria Project Brief

> This document provides a comprehensive high-level overview of Nymeria for brainstorming and architectural discussions.

## What is Nymeria?

Nymeria is a **personal AI assistant framework** built on LangGraph's ReAct (Reasoning + Acting) architecture. Unlike typical chatbots that only respond when prompted, Nymeria is designed to be a **24/7 autonomous agent** that can:

- Remember facts about the user across conversations
- Modify its own tools at runtime
- Schedule future tasks for itself
- Operate independently while the user is away
- Respect quiet hours and user preferences

**Target Use Case**: A single-user personal assistant that runs continuously, proactively helping the user rather than just reacting to commands.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         USER INPUT                                   │
│                    (CLI or REST API)                                 │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       NymeriaAgent                                   │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                 Context Injection                              │  │
│  │   • Current time (Sydney timezone)                            │  │
│  │   • Quiet hours status (10 PM - 7 AM)                         │  │
│  │   • User memories from profile                                │  │
│  │   • Personality preferences                                    │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                           │                                          │
│                           ▼                                          │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │              LangGraph ReAct Loop (max 25 iterations)         │  │
│  │                                                                │  │
│  │   ┌─────────┐    ┌─────────┐    ┌─────────────────┐          │  │
│  │   │   LLM   │───▶│ Decide  │───▶│  Execute Tool   │          │  │
│  │   │         │    │         │    │  (15 available) │          │  │
│  │   └─────────┘    └─────────┘    └────────┬────────┘          │  │
│  │        ▲                                  │                   │  │
│  │        └──────────────────────────────────┘                   │  │
│  │              (loop until task complete)                       │  │
│  └───────────────────────────────────────────────────────────────┘  │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
┌──────────────────┐ ┌──────────────┐ ┌──────────────────┐
│     SQLite       │ │ User Profile │ │    Scheduler     │
│  (Conversations) │ │  (Memories)  │ │  (Autonomous)    │
└──────────────────┘ └──────────────┘ └──────────────────┘
```

---

## Core Components

### 1. NymeriaAgent
The main orchestrator that wraps LangGraph's ReAct agent. Responsibilities:
- Loads LLM configuration (supports Anthropic, OpenAI, OpenRouter)
- Manages tool registration and hot-reloading
- Injects time context and user memories into every conversation
- Caches user-specific graphs (rebuilds when memories change)
- Provides `chat()`, `stream()`, and `astream()` methods

### 2. UserProfileManager
Thread-safe persistent storage for user data:
- **Memories**: Key-value facts about the user (name, preferences, projects)
- **Personality Overrides**: Communication style preferences (tone, verbosity)
- Storage: JSON files at `data/users/{user_id}/profile.json`
- Limits: 100 memories per user, 1000 chars per memory

**Key Behavior**: Memories are automatically injected into the system prompt. The LLM doesn't need to "recall" them - they're always present in context.

### 3. Scheduler
Manages autonomous operation via `self_invoke`:
- Background `threading.Timer` based execution
- One pending task per user (new task replaces old)
- Auto-cancels if user sends a message before task fires
- Quiet hours awareness (can suppress non-important output)

### 4. SelfModifyAgent
Sub-agent that can modify Nymeria's own tools:
- Restricted to `nymeria/tools/` directory only
- Creates backups before modifications
- Validates Python syntax before saving
- Supports: add_tool, remove_tool, fix_bug, explain

### 5. Tool System
15 tools organized into categories:

| Category | Tools | Purpose |
|----------|-------|---------|
| **Core** | bash_execute, file_read, file_write, file_list, web_search | System interaction |
| **Memory** | memory_save, memory_forget, memory_list, memory_clear_all, personality_set | User knowledge |
| **Self-Mod** | self_modify, self_modify_rollback, tools_reload | Self-improvement |
| **Scheduler** | self_invoke, mute_output | Autonomous operation |

---

## Current Autonomous Capabilities

### The self_invoke System

The primary mechanism for autonomous operation is `self_invoke`:

```python
self_invoke(prompt: str, delay: str)
# Example: self_invoke("Check if user has new emails", "30m")
```

**How it works**:
1. At the end of a conversation, Nymeria can call `self_invoke` with a future task
2. A `threading.Timer` is started for the specified delay
3. When the timer fires, Nymeria "wakes up" and executes the prompt
4. If the user messages before the timer fires, the task is auto-cancelled
5. Nymeria can chain tasks: one `self_invoke` can schedule another

**Delay formats**: `"30s"`, `"5m"`, `"1h"`, `"1d"` (max 24 hours)

### Quiet Hours

Nymeria is aware of quiet hours (currently hardcoded to 10 PM - 7 AM Sydney time):
- During quiet hours, routine check-ins can call `mute_output()` to suppress their response
- Important findings are still shown to the user
- The LLM decides what constitutes "important" based on context

### The mute_output Tool

```python
mute_output()
```

Only works during a `self_invoke` execution. When called:
- The response is still saved to conversation history
- But it won't appear in the user's terminal/UI
- Useful for routine background checks that found nothing notable

---

## Current Autonomous Behavior (from soul.md)

The system prompt instructs Nymeria to:

1. **Always schedule follow-ups**: At the end of every conversation, call `self_invoke` to check back later
2. **Be proactive**: Don't wait for the user to ask - anticipate needs
3. **Respect quiet hours**: During 10 PM - 7 AM, only surface important information
4. **Chain tasks**: One `self_invoke` can lead to another, creating continuous operation
5. **Mute when appropriate**: If a background check finds nothing, use `mute_output()`

**Example autonomous flow**:
```
User: "Remind me about my meeting tomorrow at 2pm"
Nymeria: "I'll remind you tomorrow at 1:45pm"
         [calls self_invoke("Remind user about their 2pm meeting", "23h45m")]

[23 hours 45 minutes later...]

Nymeria wakes up, sends reminder
         [calls self_invoke("Check if user needs anything else", "30m")]

[30 minutes later...]

Nymeria wakes up during quiet hours, finds nothing important
         [calls mute_output()]
         [calls self_invoke("Morning check-in", "8h")]
```

---

## Data Persistence

| Data | Storage | Lifetime |
|------|---------|----------|
| Conversations | SQLite (`data/nymeria.db`) | Permanent |
| User Memories | JSON (`data/users/{id}/profile.json`) | Permanent |
| Audit Logs | JSONL (`data/logs/audit_YYYYMMDD.jsonl`) | Permanent |
| Scheduled Tasks | SQLite (`data/tasks.db`) | Permanent (survives restarts) |
| Backups | Files (`data/backups/`) | Permanent |

**Durable Scheduler**: Scheduled tasks are now persisted to SQLite. If Nymeria restarts, pending tasks are recovered and executed automatically. Rate limiting (configurable, default 50/hour per user) prevents runaway autonomous loops.

---

## Interfaces

### CLI (`python run.py cli`)
- Interactive terminal with rich formatting
- Commands: `/history`, `/clear`, `/tools`, `/quit`
- Shows real-time streaming of responses and tool calls

### REST API (`python run.py api`)
- FastAPI server with SSE streaming
- Bearer token authentication
- Endpoints: `/chat`, `/chat/sync`, `/threads/{id}/history`, `/tools`
- Supports `user_id` for multi-user isolation

---

## Current Limitations

### Autonomous Operation Gaps

1. ~~**No persistence of scheduled tasks**~~: **RESOLVED** - Tasks now persist to SQLite (`data/tasks.db`) and recover on restart.

2. **No external trigger**: Nymeria can only wake up from `self_invoke`. There's no way for external events (new email, file change, webhook) to trigger her.

3. **No supervisor/manager**: Nothing monitors whether Nymeria is healthy, stuck, or has stopped scheduling tasks.

4. **Single-threaded execution**: Only one `self_invoke` can be pending per user. Complex parallel monitoring isn't possible.

5. **No priority system**: All `self_invoke` tasks are equal. An urgent reminder competes with routine check-ins.

6. **Hardcoded quiet hours**: 10 PM - 7 AM Sydney time is hardcoded. Not configurable per user.

7. ~~**No retry on failure**~~: **RESOLVED** - Failed tasks now retry up to 3 times before being marked as failed.

8. **Limited delay**: Maximum `self_invoke` delay is 24 hours.

9. ~~**No rate limiting**~~: **RESOLVED** - Rate limiting (configurable, default 50/hour per user) prevents runaway loops.

### Memory Limitations

1. **100 memory limit**: Each user can only store 100 memories
2. **No summarization**: Old memories must be manually deleted
3. **No semantic search**: Can't search memories by meaning, only list all

### Self-Modification Limitations

1. **Tools only**: Can only modify `nymeria/tools/`, not core agent or config
2. **No automatic testing**: Modified tools aren't automatically tested
3. **Manual reload**: Must call `tools_reload()` after modification

---

## Technical Stack

| Component | Technology |
|-----------|------------|
| Agent Framework | LangGraph (custom ReAct implementation) |
| LLM | Claude (Anthropic), GPT-4 (OpenAI), or via OpenRouter |
| Tool Abstraction | LangChain `@tool` decorator |
| Conversation Storage | SQLite via langgraph-checkpoint-sqlite |
| API Server | FastAPI with SSE |
| Settings | Pydantic Settings (from .env) |
| Scheduling | Python `threading.Timer` |

---

## File Structure

```
C:\Nymeria\
├── nymeria/
│   ├── core/
│   │   ├── agent.py          # Main NymeriaAgent class
│   │   ├── user_profile.py   # Memory/profile management
│   │   ├── scheduler.py      # self_invoke scheduling
│   │   ├── self_agent.py     # Self-modification sub-agent
│   │   ├── backup.py         # Backup system
│   │   └── validator.py      # Python code validation
│   ├── tools/
│   │   ├── bash.py           # Shell execution
│   │   ├── filesystem.py     # File operations
│   │   ├── web.py            # Web search
│   │   ├── memory.py         # Memory tools
│   │   ├── self_modify.py    # Self-modification tools
│   │   ├── scheduler.py      # Scheduler tools
│   │   └── utils.py          # Shared helpers
│   ├── triggers/
│   │   ├── cli.py            # Terminal interface
│   │   ├── api.py            # REST API
│   │   └── base.py           # Base trigger class
│   └── config/
│       ├── settings.py       # Pydantic settings
│       ├── soul.md           # System prompt
│       └── self_agent_prompt.md
├── data/
│   ├── nymeria.db            # Conversations
│   ├── users/                # User profiles
│   ├── logs/                 # Audit logs
│   └── backups/              # Self-mod backups
└── docs/
    ├── architecture.md
    ├── tools.md
    ├── configuration.md
    └── api.md
```

---

## Key Design Decisions

1. **LLM-driven autonomy**: The LLM decides when/what to schedule, not hardcoded rules
2. **Memory in context**: Memories are injected into system prompt, not recalled via tool
3. **Single user focus**: Designed for personal use, multi-user is secondary
4. **Self-modification sandboxed**: Can only modify tools, not core functionality
5. **Streaming first**: All interfaces support streaming responses
6. **Audit everything**: All tool calls are logged for transparency

---

## Questions for Brainstorming

When discussing improvements to autonomous operation, consider:

1. **Reliability**: How do we ensure Nymeria keeps running and doesn't "forget" scheduled tasks?
2. **External triggers**: How could external events (webhooks, file watchers, emails) wake Nymeria?
3. **Supervision**: Should there be a "manager" that monitors Nymeria's health and behavior?
4. **Priority**: How should urgent vs routine tasks be handled?
5. **Parallelism**: Should multiple autonomous tasks run concurrently?
6. **Recovery**: What happens when things fail? How does Nymeria recover?
7. **User control**: How much control should users have over autonomous behavior?
8. **Resource limits**: How do we prevent runaway autonomous loops?
