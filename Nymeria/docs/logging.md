# Logging Reference

Most runtime logging is centralized in `nymeria/config/logging_config.py`. For normal CLI, API, worker, bot, and foreground gateway runs, log level decisions flow through `configure_logging()`, called by `run.py`. Format: `HH:MM:SS LEVEL module: message`.

**Log file:** `Nymeria/data/logs/service.log` (rotating; size and backup count come from settings). In normal `run.py` flows, logs go to both console and this file.

```bash
# Read logs from a different terminal while API runs:
tail -f Nymeria/data/logs/service.log
tail -100 Nymeria/data/logs/service.log

# Filter by tag or thread:
grep '\[CALLABLE\]' Nymeria/data/logs/service.log
grep '\[LLM\]' Nymeria/data/logs/service.log
grep 'thread=abc-123' Nymeria/data/logs/service.log
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

### MCP server mode

`nymeria/mcp_server.py` sets up its own basic stderr logging with `logging.basicConfig(...)` so STDIO JSON-RPC is not corrupted. Treat MCP logging as a separate path from the main application runtime.

## Log Tags

Every subsystem uses a standardized `[TAG]` prefix. Filter by tag to isolate a subsystem:

| Tag | Source file | Meaning |
|-----|-------------|---------|
| `[LLM]` | `vendor/react_agent/nodes.py` | LLM invocation and response. INFO shows message count + response summary. DEBUG shows full message array. |
| `[LLM STREAM]` | `vendor/react_agent/nodes.py` | Async model-stream diagnostics. Shows provider chunk counts, first-chunk latency, text/reasoning/tool-call chunk counts, and whether the async node had to fall back to `ainvoke()`. |
| `[ASTREAM]` | `core/agent.py` `astream()` | Agent streaming path. Used directly by `/chat` and via `iter_agent_astream()` for sync workers such as ticker, triggers, callable threads, and CLI. |
| `[ASTREAM DIAG]` | `core/agent.py` `astream()` | Per-graph diagnostics for whether LangGraph emitted `on_chat_model_stream` events or fell back to full `on_chat_model_end` content. INFO for autonomous runs, DEBUG for regular user chat unless a warning condition occurs. |
| `[STREAM_BRIDGE]` | `core/stream_bridge.py` | Sync bridge diagnostics for autonomous worker callers. Shows when a sync worker starts consuming `agent.astream()`, first yielded chunk timing/type, total yielded chunks, and elapsed time. |
| `[CALLABLE]` | `core/thread_agent_executor.py` | Callable thread lifecycle. Shows name, thread_id, task_id, task preview, timing. |
| `[TICKER]` | `core/ticker.py` | Scheduled TODO execution. |
| `[WATCHDOG]` | `triggers/watchdog_worker.py` | Stale TODO nudge lifecycle. Emitted by the standalone watchdog container; the API also logs `[ASTREAM]` when the resulting `/chat` self-invoke runs. |
| `[TRIGGER]` | `core/trigger_manager.py` | Event-driven trigger firing. Shows trigger name, thread_id, timing. |

**Framing pattern:** Every execution path uses `=== START ===` / `=== END ===` / `=== ERROR ===` framing at INFO:
- **START** always includes: `thread=`, `user=`, and a description of what's happening
- **END** always includes: `thread=`, `elapsed=Xs`, and a summary metric (chunks, response_len, etc.)
- **ERROR** always includes: `thread=`, `elapsed=Xs`, and the exception

## How to Read Logs  -  Common Scenarios

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
[LLM] Invoking with 3 messages (0 tool rounds)       ← this is inside the callable's astream()
[LLM] Response: 0 chars, final answer                 ← empty response = LLM returned nothing
[CALLABLE] === END === name=ResearchAgent, ..., response_len=0, elapsed=3.1s
```
Key: `response_len=0` confirms the callable returned empty. Check the nested `[ASTREAM] === START ===` line for the callable's thread_id to see its full stream lifecycle.

**4. "Thread orchestration  -  which thread called which?"**
When a main thread invokes callable threads, the log interleaves but each line has its `thread=` identifier:
```
[ASTREAM] === START === thread=main-abc, user=default              ← user's chat
[LLM] Response: tool_calls=['ResearchAgent', 'CodeAgent']          ← main agent calls two callables
[CALLABLE] === START === name=ResearchAgent, thread=research-123   ← child 1 starts
[CALLABLE] === START === name=CodeAgent, thread=code-456           ← child 2 starts (concurrent)
[ASTREAM] === START === thread=research-123, holder=user           ← child 1's inner stream
[ASTREAM] === START === thread=code-456, holder=user               ← child 2's inner stream
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

**6. "User clicked stop  -  did it cascade?"**
Look for the cascade chain:
```
[ASTREAM] Thread main-abc: Aborted by cancel signal               ← user's thread stopped
Cascading abort from thread main-abc to child research-123         ← cascade to child
[ASTREAM] Thread research-123: Aborted by cancel signal            ← child stopped
```

**7. "Autonomous task (ticker/watchdog/trigger)  -  what happened?"**
Autonomous tasks use `[ASTREAM]` with `holder=autonomous`, even when the caller is a sync worker using `iter_agent_astream()`:
```
[STREAM_BRIDGE] start thread=todo-thread user=default autonomous=True
[ASTREAM] === START === thread=todo-thread, user=default, holder=autonomous
[LLM STREAM] async_complete chunks=42 text_chunks=18 text_chars=300 reasoning_chunks=12 reasoning_chars=900 tool_call_chunk_events=1 first_chunk_ms=650 elapsed_ms=5200
[ASTREAM DIAG] graph_done thread=todo-thread autonomous=True model_calls=2 model_stream_events=43 model_end_without_stream=0 model_end_fallbacks=0 elapsed_ms=9000
[STREAM_BRIDGE] end thread=todo-thread chunks=25 elapsed_ms=12300
[WATCHDOG] === START === thread=todo-thread, stale_todos=2
...
[WATCHDOG] === END === thread=todo-thread, chunks=5, response_len=200, elapsed=12.3s
[ASTREAM] === END === thread=todo-thread, elapsed=12.5s
```
Note the nested framing: `[WATCHDOG]` wraps the high-level nudge, `[ASTREAM]` wraps the inner agent execution.

If autonomous output looks batched, compare these diagnostics:
- `[LLM STREAM] chunks=1` with a large `text_chars` value means the provider or LangChain model wrapper only delivered one coarse async chunk.
- `[LLM STREAM] chunks>1` but `[ASTREAM DIAG] model_stream_events=0` means LangGraph did not surface the provider chunks.
- `[ASTREAM DIAG] model_stream_events>1` but `[STREAM_BRIDGE] chunks` is low means Nymeria's SSE conversion or autonomous bridge is dropping/coalescing events.
- `[ASTREAM DIAG] inline_thinking_possible` followed by `inline_thinking_buffer_hold` means Nymeria saw an empty provider reasoning block and is temporarily holding normal text to verify it is not leaked `<think>` content.
- `inline_thinking_buffer_release` means that held text was released before model end; `inline_thinking_buffer_flush` means it was released at `on_chat_model_end`, which can make pre-tool preamble appear right before the first tool card.

If the worker streamed chunks but the desktop did not update live, trace the autonomous SSE path hop by hop:
```
[REDIS EVENT BUS] publish type=response count=1 redis_receivers=...
[REDIS EVENT BUS] message_received type=response count=1 local_subscribers=...
[REDIS EVENT BUS] queue_enqueue subscriber=... type=response count=1
[AUTONOMOUS SSE] queue_receive subscriber=... type=response count=1
[AUTONOMOUS SSE] yield subscriber=... type=response count=1
[Autonomous] First stream byte received ...
[Autonomous] First data event frame received ...
[Autonomous] Event handled type=response count=1 total=...
```

Interpretation:
- Worker publish present, but API `message_received` absent: Redis pub/sub or API subscriber thread problem.
- API `message_received` has `local_subscribers=0`: no active `/autonomous/stream` client in that API process.
- `queue_enqueue` present, but no `queue_receive`/`yield`: SSE generator or subscriber queue stalled.
- `yield` present, but no desktop `Event handled`: browser stream connection, auth, network, or frontend parser/reconnect issue.
- Desktop `idle_timeout` followed by reconnect is expected if heartbeat/data bytes stop arriving for the watchdog window; it should reconnect and refresh current history/context.
