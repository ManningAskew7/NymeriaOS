# Archived Plan: Self-Improvement Tools for Nymeria

## Overview

This is an archived planning document for early self-improvement ideas. Large parts of the memory system, self-modification safety layer, backups, rollback flow, and custom-tool architecture have since been implemented in different forms.

---

## Tool 1: Memory & Personality System

Status: largely implemented in evolved form (`user_profile.py`, memory tools, personality preferences, prompt injection of profile context).

### Purpose
Allow Nymeria to remember user preferences and adapt her personality across conversations.

### Components

#### 1.1 User Profile Storage (`data/users/{user_id}/profile.json`)

```json
{
  "user_id": "default",
  "name": "Alex",
  "preferences": {
    "communication_style": "concise",
    "timezone": "Australia/Sydney",
    "interests": ["crypto", "programming"]
  },
  "memories": [
    {"timestamp": "...", "key": "favorite_color", "value": "blue"},
    {"timestamp": "...", "key": "project", "value": "working on Nymeria"}
  ],
  "opt_in": {
    "memory_enabled": true,
    "personality_adaptation": true
  }
}
```

#### 1.2 Tools

**`memory_save`** - Store a fact about the user
```python
memory_save(key: str, value: str) -> str
# Example: memory_save("favorite_language", "Python")
```

**`memory_recall`** - Retrieve stored memories
```python
memory_recall(query: Optional[str] = None) -> str
# Returns relevant memories, or all if no query
```

**`memory_forget`** - Delete a memory (user request)
```python
memory_forget(key: str) -> str
```

**`personality_set`** - Modify system prompt preferences
```python
personality_set(trait: str, value: str) -> str
# Example: personality_set("tone", "casual and friendly")
```

#### 1.3 Opt-In System

- First conversation: Ask user if they want Nymeria to remember things
- Store preference in profile
- Respect `opt_in.memory_enabled` before saving
- User can say "forget everything about me" to clear

#### 1.4 Implementation

1. Create `nymeria/core/user_profile.py` - Profile management
2. Create `nymeria/tools/memory.py` - Memory tools
3. Inject relevant memories into system prompt on each turn
4. Store profiles in `data/users/` directory

---

## Tool 2: Self-Modification Agent

Status: partially implemented and evolved into the current self-modification/callable-thread toolchain with backups, rollback support, restricted file handling, and dedicated prompts.

### Purpose
A sub-agent that Nymeria can invoke to modify her own configuration, add tools, or debug issues.

### Architecture

```
┌─────────────────────────────────────────────┐
│              Main Nymeria Agent             │
│                                             │
│   "I need to add a new tool..."             │
│            │                                │
│            ▼                                │
│   ┌─────────────────────────────────┐       │
│   │    self_modify(instruction)     │       │
│   └─────────────────────────────────┘       │
└─────────────────────┬───────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────┐
│           Self-Modification Agent           │
│                                             │
│  Context includes:                          │
│  - Nymeria's codebase structure             │
│  - LangGraph documentation                  │
│  - Current configuration                    │
│  - Tool creation guidelines                 │
│                                             │
│  Tools available:                           │
│  - file_read, file_write                    │
│  - bash_execute (for testing)               │
│  - code_validate (syntax check)             │
└─────────────────────────────────────────────┘
```

### Tool Definition

**`self_modify`** - Invoke the self-modification agent
```python
self_modify(instruction: str, category: Literal["add_tool", "fix_bug", "modify_config", "explain"]) -> str
```

**Categories:**
- `add_tool`: Create a new tool based on description
- `fix_bug`: Diagnose and fix an issue Nymeria is experiencing
- `modify_config`: Change settings, system prompt, etc.
- `explain`: Explain how something in Nymeria works

### Sub-Agent Context

The self-modification agent gets a specialized system prompt with:

1. **Codebase overview** - Key files and their purposes
2. **Tool creation template** - How to properly define tools
3. **LangGraph patterns** - ReAct loop, state management
4. **Safety guidelines** - What it should/shouldn't modify

### Safety Measures

1. **Backup before modify** - Auto-backup files before changes
2. **Syntax validation** - Check Python syntax before saving
3. **Test after changes** - Run `test_nymeria.py --quick` after modifications
4. **Rollback capability** - Keep backups to restore if tests fail
5. **Restricted paths** - Can only modify files in `nymeria/` directory

### Implementation

1. Create `nymeria/tools/self_modify.py` - Main tool
2. Create `nymeria/core/self_agent.py` - Sub-agent implementation
3. Create `nymeria/config/self_agent_prompt.md` - Specialized system prompt
4. Add backup/restore utilities

---

## Implementation Phases

### Phase 1: Memory System
- [ ] Create user profile storage
- [ ] Implement memory_save, memory_recall, memory_forget tools
- [ ] Add opt-in flow on first conversation
- [ ] Inject memories into system prompt

### Phase 2: Personality Adaptation
- [ ] Create personality_set tool
- [ ] Store personality overrides in profile
- [ ] Merge user preferences with base soul.md

### Phase 3: Self-Modification Agent
- [ ] Create sub-agent with specialized context
- [ ] Implement self_modify tool
- [ ] Add safety measures (backup, validate, test)
- [ ] Test with "add a calculator tool" request

### Phase 4: Polish
- [ ] Add "forget me" command
- [ ] Add "show what you know about me" command
- [ ] Create admin commands for managing profiles
- [ ] Documentation

---

## Example Interactions

### Memory System

```
User: My name is Alex and I prefer concise responses
Nymeria: Got it, Alex! I'll remember that and keep my responses concise.
         [Saves to memory]

--- Next conversation ---

User: Hello
Nymeria: Hi Alex! What can I help with?
         [Loaded memory: name=Alex, style=concise]
```

### Self-Modification

```
User: Can you add a tool that converts currencies?
Nymeria: I'll create that tool for you.
         [Invokes self_modify("create currency conversion tool", "add_tool")]

         Done! I've added a `currency_convert` tool. Let me test it...
         [Tests the new tool]

         Working! You can now ask me to convert currencies.
```

---

## Files to Create

| File | Purpose |
|------|---------|
| `nymeria/core/user_profile.py` | Profile management |
| `nymeria/tools/memory.py` | Memory tools |
| `nymeria/tools/self_modify.py` | Self-modification tool |
| `nymeria/core/self_agent.py` | Sub-agent implementation |
| `nymeria/config/self_agent_prompt.md` | Sub-agent system prompt |
| `data/users/.gitkeep` | User profiles directory |

---

## Notes for Readers

Treat file paths, tool names, and architecture in this document as historical planning context, not current source-of-truth documentation. For current behavior, prefer `docs/architecture.md`, `docs/tools.md`, `docs/api.md`, and the runtime code under `nymeria/core/`, `nymeria/tools/`, and `nymeria/triggers/`.

## Open Questions

1. **Multi-user support**: Should profiles be tied to API keys or thread_ids?
2. **Memory limits**: How many memories per user? Summarize old ones?
3. **Self-modify scope**: Should Nymeria be able to modify core files or only tools?
4. **Approval flow**: Should some self-modifications require user approval?
