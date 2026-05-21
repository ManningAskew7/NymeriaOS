# Tool-Call Formulation Streaming Investigation

**Date:** 2026-05-21
**Status:** Open follow-up. Finish by surfacing the first streamed tool-call
chunk to the desktop activity indicator.

## Problem

The desktop activity indicator currently starts a turn as `Processing...`.
When no thinking tokens or response text arrive for about one second, the
frontend guesses that the model may be formulating a tool call and switches to
`Formulating...`.

That guess exists because tool-call JSON is not visible answer text. The model
may be streaming tool-call arguments to the provider adapter while the UI sees
no text, thinking, or tool invocation yet.

## Provider Facts

Anthropic Messages streaming has provider events before visible response text:

- `message_start` begins an assistant response stream.
- `content_block_start` begins a content block.
- Tool calls begin with `content_block_start` where the block type is
  `tool_use`.
- Tool-call arguments stream as `content_block_delta` with
  `delta.type == "input_json_delta"` and partial JSON.

For UI status, `content_block_start` with `tool_use` is the meaningful signal.
If surfaced, the UI can switch to `Formulating...` immediately instead of
waiting for a quiet timer or the later tool invocation event.

## Local Capture

Capture ran on the Docker API container through CLIProxy with thinking disabled
and a forced `file_write` tool call. The installed provider stack at the time:

- `langchain-anthropic 1.4.2`
- `anthropic 0.97.0`
- Provider `anthropic`, model `claude-opus-4-6`, routed through CLIProxy

Temporary logging setup:

```bash
cd /opt/NymeriaOS/Nymeria
DISCORD_BOT_TOKEN=disabled \
LOG_LEVEL=INFO \
LOG_PROFILES= \
LOG_MODULES=nymeria.core.agent_streaming:DEBUG \
docker compose --env-file .env.docker up -d --no-deps --force-recreate api
```

Filtered capture:

```bash
docker logs -f --since 30s nymeria-api 2>&1 \
  | rg --line-buffered "ASTREAM DIAG|LLM STREAM|tool_start|tool_end|\[CHAT\]|Response:"
```

Important lines:

```text
[LLM STREAM] async_complete chunks=88 text_chunks=0 text_chars=0 reasoning_chunks=0 reasoning_chars=0 tool_call_chunk_events=86 first_chunk_ms=3167 elapsed_ms=7238
[LLM] Response: 773 chars, tool_calls=['file_write']
[LLM STREAM] async_complete chunks=4 text_chunks=2 text_chars=46 reasoning_chunks=0 reasoning_chars=0 tool_call_chunk_events=0 first_chunk_ms=2449 elapsed_ms=2886
[LLM] Response: 88 chars, final answer
```

Logging was restored afterward:

```bash
cd /opt/NymeriaOS/Nymeria
DISCORD_BOT_TOKEN=disabled \
LOG_LEVEL=INFO \
LOG_PROFILES= \
LOG_MODULES= \
docker compose --env-file .env.docker up -d --no-deps --force-recreate api
```

## Findings

- CLIProxy did preserve streamed tool-call chunks for this path.
- The async agent node saw `86` tool-call chunk events before the `file_write`
  invocation.
- The first model stream chunk reached the async node after about `3.2s`.
- No `ASTREAM DIAG` lines appeared from `nymeria.core.agent_streaming`, even
  with that module set to `DEBUG`.
- This suggests the streamed chunks are visible inside
  `nymeria/vendor/react_agent/nodes.py::async_agent_node`, but are not being
  surfaced through Nymeria's SSE conversion layer early enough for the desktop
  status UI.

## Recommended Follow-Up

Implement the first signal in the async agent node, not in upstream LangGraph
internals.

- In `nymeria/vendor/react_agent/nodes.py::create_agent_node`, let
  `async_agent_node` accept the runnable config if needed for LangChain custom
  events.
- While iterating `candidate.astream(messages_with_system)`, detect the first
  tool signal in the current model call:
  - `chunk.tool_call_chunks` is truthy, or
  - `chunk.content` contains a block with type `tool_use`,
    `input_json_delta`, `tool_call`, `tool_call_chunk`, or `function_call`.
- Emit one custom event named `tool_call_delta` with an empty payload using
  LangChain's `adispatch_custom_event`.
- Keep the existing backend and frontend behavior idempotent. A duplicate
  `tool_call_delta` should only keep the activity phase at `Formulating...`.
- Do not log prompt text, tool arguments, or tool result bodies while testing
  this status signal.

Test coverage should include:

- A helper test for detecting tool-call chunks from `tool_call_chunks`.
- A helper test for detecting provider content blocks such as `tool_use` and
  `input_json_delta`.
- A streaming path test proving `tool_call_delta` can pass through
  `GraphStreamProcessor._handle_custom_event`.
- A desktop store or component test proving `tool_call_delta` sets the assistant
  activity phase to `formulating` before the later `tool_call` event.
