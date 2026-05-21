# core/

Runtime heart of Nymeria. Agent orchestration, streaming, persistence, and all supporting subsystems.

## Start here

`agent.py`  -  main NymeriaAgent class. Large file; splitting is planned but out of scope for now.

## Contents

- `agent.py`  -  agent orchestration, thread locks, graph execution
- `stream_bridge.py`  -  async streaming bridge between agent and API
- `agent_compaction.py`  -  context compaction logic
- `command_service.py`  -  slash command dispatcher (large file)
- `todo_manager.py`  -  TODO scheduling, watchdog integration
- `mcp_runtime.py`, `mcp_manager.py`  -  MCP server lifecycle
- `accounts.py`  -  multi-user account management
- `event_bus.py`, `event_bus_redis.py`  -  pub/sub event system
- `ticker.py`  -  autonomous task runner
- `config/` files: `thread_config.py`, `credential_vault.py`, `secrets.py`

## Notes

Several files here are large (agent.py ~177KB, command_service.py ~111KB). Splitting is tracked separately and should not be attempted as part of routine changes.
