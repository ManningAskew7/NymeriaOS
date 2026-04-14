# Logging Reference

Most runtime logging is centralized in `nymeria/config/logging_config.py`. For normal CLI, API, worker, bot, and foreground gateway runs, log level decisions flow through `configure_logging()`, called by `run.py`. Format: `HH:MM:SS LEVEL module: message`.

**Log file:** `Nymeria/data/logs/service.log` (rotating; size and backup count come from settings). In normal `run.py` flows, logs go to both console and this file.

**Important exception:** the Windows service path in `service_runner.py` uses its own simpler logging setup instead of `configure_logging()`.

```bash
# Read logs from a different terminal while API runs:
tail -f /mnt/c/NymeriaOS/Nymeria/data/logs/service.log
tail -100 /mnt/c/NymeriaOS/Nymeria/data/logs/service.log

# Filter by tag or thread:
grep '\[CALLABLE\]' /mnt/c/NymeriaOS/Nymeria/data/logs/service.log
grep '\[LLM\]' /mnt/c/NymeriaOS/Nymeria/data/logs/service.log
grep 'thread=abc-123' /mnt/c/NymeriaOS/Nymeria/data/logs/service.log
```

## Environment Variables

```bash
LOG_LEVEL=INFO                          # Base level for nymeria.* loggers
LOG_PROFILES=llm,threads                # Named debug profiles (comma-separated)
LOG_MODULES=nymeria.core.agent:DEBUG    # Per-module overrides (highest priority)
```

## Available Profiles

| Profile | What it enables | When to use |
|---------|----------------|-------------|
| `llm` | Full message arrays sent to LLM, provider decisions | Debug wrong/missing LLM responses, tool binding issues |
| `tools` | Tool call/result tracing, callable thread executor | Debug tool failures, unexpected tool behavior |
| `agent` | Stream lifecycle, context loading, lock details | Debug stream hangs, lock contention, context issues |
| `threads` | Callable thread executor + tool_factory + agent | **Debug callable thread orchestration and parent→child flows** |
| `ticker` | Scheduled TODO polling and execution, watchdog | Debug autonomous task scheduling |
| `triggers` | Trigger checking, firing, source plugins | Debug event-driven trigger issues |
| `checkpoints` | SQLite/Postgres checkpoint read/write | Debug state persistence, missing messages |
| `api` | HTTP request handling | Debug API routing, auth issues, schedule parsing |
| `sse` | Event bus publishing | Debug frontend not receiving events |
| `compactor` | Auto-compaction, token tracking | Debug context management |
| `all` | Everything at DEBUG | Full firehose (very verbose) |

## Special Cases

### Windows service mode

`service_runner.py` does **not** use the centralized formatter/profile system. It configures a plain rotating file handler directly, writes only to file, and suppresses `httpx`/`httpcore` noise separately.

That means:
- `LOG_PROFILES` and `LOG_MODULES` are documented for the main `run.py` startup paths, not this service runner path
- service log formatting differs from the compact `NymeriaFormatter`
- if you are debugging the Windows service specifically, check `service_runner.py` first

### MCP server mode

`nymeria/mcp_server.py` sets up its own basic stderr logging with `logging.basicConfig(...)` so STDIO JSON-RPC is not corrupted. Treat MCP logging as a separate path from the main application runtime.

## Log Tags

Every subsystem uses a standardized `[TAG]` prefix. Filter by tag to isolate a subsystem:

| Tag | Source file | Meaning |
|-----|-------------|---------|
| `[LLM]` | `vendor/react_agent/nodes.py` | LLM invocation and response. INFO shows message count + response summary. DEBUG shows full message array. |
| `[STREAM]` | `core/agent.py` `stream()` | Sync streaming path (used by ticker, watchdog, triggers, callable threads). |
| `[ASTREAM]` | `core/agent.py` `astream()` | Async streaming path (used by the API for user chat). |
| `[CALLABLE]` | `core/thread_agent_executor.py` | Callable thread lifecycle. Shows name, thread_id, task_id, task preview, timing. |
| `[TICKER]` | `core/ticker.py` | Scheduled TODO execution. |
| `[WATCHDOG]` | `core/watchdog.py` | Stale TODO nudge lifecycle. |
| `[TRIGGER]` | `core/trigger_manager.py` | Event-driven trigger firing. Shows trigger name, thread_id, timing. |

**Framing pattern:** Every execution path uses `=== START ===` / `=== END ===` / `=== ERROR ===` framing at INFO:
- **START** always includes: `thread=`, `user=`, and a description of what's happening
- **END** always includes: `thread=`, `elapsed=Xs`, and a summary metric (chunks, response_len, etc.)
- **ERROR** always includes: `thread=`, `elapsed=Xs`, and the exception

## How to Read Logs — Common Scenarios

**1. "What happened during a user chat message?"**
Look for the `[ASTREAM]` START→END pair matching the thread_id:
```
[ASTREAM] === START === thread=abc-123, user=default
[LLM] Invoking with 5 messages (0 tool rounds)
[LLM] Response: 200 chars, tool_calls=['web_search']
[LLM] Invoking with 7 messages (1 tool rounds)
[LLM] Response: 500 chars, final answer
[ASTREAM] === END === thread=abc-123, elapsed=8.2s
```
The `[LLM]` lines between START/END show each ReAct loop iteration. "tool rounds" = how many tool calls happened before this LLM call. "final answer" = no more tool calls, the agent is done.

**2. "What did the LLM actually see?"**
Set `LOG_PROFILES=llm`. This enables DEBUG-level message dumps showing every message in the context array:
```
[LLM]   [0] HumanMessage: What's the weather in Sydney?...
[LLM]   [1] AIMessage [tools: web_search]: Let me check...
[LLM]   [2] ToolMessage: {"temperature": 22, ...}...
```

**3. "A callable thread returned an empty/wrong response"**
Set `LOG_PROFILES=threads`. Trace the callable thread by its name:
```
[CALLABLE] === START === name=ResearchAgent, thread=research-xyz, task_id=callable-ResearchAgent-a1b2c3d4, user=default, task=Find the latest pricing...
[LLM] Invoking with 3 messages (0 tool rounds)       ← this is inside the callable's stream()
[LLM] Response: 0 chars, final answer                 ← empty response = LLM returned nothing
[CALLABLE] === END === name=ResearchAgent, ..., response_len=0, elapsed=3.1s
```
Key: `response_len=0` confirms the callable returned empty. Check the `[STREAM] === START ===` line nested inside for the callable's thread_id to see its full stream lifecycle.

**4. "Thread orchestration — which thread called which?"**
When a main thread invokes callable threads, the log interleaves but each line has its `thread=` identifier:
```
[ASTREAM] === START === thread=main-abc, user=default              ← user's chat
[LLM] Response: tool_calls=['ResearchAgent', 'CodeAgent']          ← main agent calls two callables
[CALLABLE] === START === name=ResearchAgent, thread=research-123   ← child 1 starts
[CALLABLE] === START === name=CodeAgent, thread=code-456           ← child 2 starts (concurrent)
[STREAM] === START === thread=research-123, holder=user            ← child 1's inner stream
[STREAM] === START === thread=code-456, holder=user                ← child 2's inner stream
...interleaved [LLM] calls from both children...
[CALLABLE] === END === name=ResearchAgent, elapsed=6.2s            ← child 1 finishes
[CALLABLE] === END === name=CodeAgent, elapsed=8.1s                ← child 2 finishes
[ASTREAM] === END === thread=main-abc, elapsed=14.2s               ← main thread finishes
```
To trace one thread through interleaved logs, grep for its thread_id.

**5. "A tool timed out"**
Look for `[LLM] TRUNCATED` (max_tokens hit) or check `SafeToolNode` timeout logs:
```
Tool execution timed out after 300s. Tools: ['web_search']
```
If a callable thread tool times out, the parent thread's `_on_tool_timeout` fires and signals abort on the callable's thread_id, cascading to any children.

**6. "User clicked stop — did it cascade?"**
Look for the cascade chain:
```
[ASTREAM] Thread main-abc: Aborted by cancel signal               ← user's thread stopped
Cascading abort from thread main-abc to child research-123         ← cascade to child
[STREAM] Thread research-123: Aborted by cancel signal             ← child stopped
```

**7. "Autonomous task (ticker/watchdog/trigger) — what happened?"**
Autonomous tasks use `[STREAM]` (sync path) with `holder=autonomous`:
```
[STREAM] === START === thread=todo-thread, user=default, holder=autonomous
[WATCHDOG] === START === thread=todo-thread, stale_todos=2
...
[WATCHDOG] === END === thread=todo-thread, chunks=5, response_len=200, elapsed=12.3s
[STREAM] === END === thread=todo-thread, chunks=5, elapsed=12.5s
```
Note the nested framing: `[WATCHDOG]` wraps the high-level nudge, `[STREAM]` wraps the inner `agent.stream()` call.
