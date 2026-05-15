# routers/

FastAPI route slices. One router per domain, matched by filename.

## Start here

`chat.py` — main chat endpoint (SSE streaming). `threads.py` — thread CRUD.

## Contents

- `chat.py` — chat + SSE streaming
- `threads.py`, `thread_config.py`, `thread_operations.py`, `agent_threads.py` — thread management
- `accounts.py`, `credentials.py` — auth and accounts
- `todos.py` — TODO CRUD
- `tools.py`, `unified_tools.py`, `user_tools.py`, `custom_tools.py` — tool management
- `skills.py` — skill management
- `mcp_servers.py` — MCP server management
- `settings.py` — runtime settings
- `voice.py`, `workspace.py`, `devices.py`, `rag.py`, `activity.py`, `memory.py`, `commands.py`, `chat_apps.py`, `autonomous_stream.py`, `system.py`
