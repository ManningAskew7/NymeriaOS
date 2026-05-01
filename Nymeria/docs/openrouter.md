# OpenRouter Provider Integration

How Nymeria uses OpenRouter, with enough implementation detail to debug
Responses API, Chat Completions, reasoning streams, tool calls, and replayed
history.

**Status:** current as of 2026-04-27. OpenRouter's Responses API is still beta,
so verify live docs before changing provider code.

Primary references:

- OpenRouter API overview: https://openrouter.ai/docs/api/reference/overview
- OpenRouter Chat Completions endpoint: https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request
- OpenRouter Responses overview: https://openrouter.ai/docs/api/reference/responses/overview
- OpenRouter Responses basic usage: https://openrouter.ai/docs/api/reference/responses/basic-usage
- OpenRouter Responses reasoning: https://openrouter.ai/docs/api/reference/responses/reasoning
- OpenRouter Responses tool calling: https://openrouter.ai/docs/api/reference/responses/tool-calling
- OpenAPI schema: https://openrouter.ai/openapi.json

## TL;DR

- Nymeria's OpenRouter provider has two API modes:
  - `responses` - default. Uses OpenRouter's beta `/api/v1/responses` endpoint.
  - `chat_completions` - opt-in compatibility mode. Uses `/api/v1/chat/completions`.
- Nymeria does not pass its thread ID as an OpenRouter session ID.
- The LangGraph checkpoint is the source of truth. Each turn loads checkpointed
  messages for the Nymeria thread and sends the provider a full conversation
  payload.
- OpenRouter Responses beta is stateless. Nymeria sets `store=false`, does not
  use `previous_response_id`, and includes full history on every request.
- OpenRouter Chat Completions is also history-replay based, but uses the older
  OpenAI-compatible `messages` shape instead of Responses `input` items.
- Thinking text shown in the frontend is provider-native reasoning text surfaced
  by Nymeria. It is not guaranteed to be readable by the model on later turns.
  Do not rely on a model being able to quote prior hidden thinking.

## Configuration

Global settings live in `Nymeria/nymeria/config/settings.py`. Per-thread settings
come from `ThreadConfig` and are folded into the runtime `LLMConfig` in
`Nymeria/nymeria/core/agent.py`.

Important fields:

| Field | Meaning |
| --- | --- |
| `LLM_PROVIDER=openrouter` | Use OpenRouter as the LLM provider. |
| `OPENROUTER_API_KEY` | Bearer token used for OpenRouter. |
| `LLM_MODEL` / per-thread `model` | OpenRouter model slug, for example `anthropic/claude-haiku-4.5`. |
| `OPENAI_API_MODE=responses` | Default OpenRouter mode. Uses `/api/v1/responses`. |
| `OPENAI_API_MODE=chat_completions` | Opt out of Responses beta. Uses `/api/v1/chat/completions`. |
| `LLM_EXTENDED_THINKING=true` | Request reasoning when the selected model supports it. |
| `LLM_REASONING_EFFORT=low|medium|high` | Reasoning effort sent to OpenRouter when supported. |
| `LLM_USE_MODEL_DEFAULTS=true` | Suppress sampling parameters and let the provider/model decide defaults. |
| `LLM_BASE_URL` | Normally not set for OpenRouter. If set globally for another provider, per-thread OpenRouter should resolve back to `https://openrouter.ai/api/v1` unless explicitly overridden. |

The stored field name is still `openai_api_mode` because the same setting is used
for OpenAI-compatible providers. In UI copy it should be described as "API Mode"
or "OpenAI-compatible API Mode", not as a direct-OpenAI-only setting.

## State Model

Nymeria is stateful; OpenRouter is not.

The runtime flow is:

```text
frontend thread_id
  -> Nymeria API
  -> LangGraph checkpoint lookup by thread_id
  -> messages/tool calls/reasoning blocks rebuilt from checkpoint
  -> one full OpenRouter request
  -> response streamed back to frontend
  -> new AIMessage/tool messages written back to checkpoint
```

The provider flow is not:

```text
frontend thread_id
  -> OpenRouter session id
  -> provider remembers thread history
```

This distinction matters for debugging. If a model forgets previous user-visible
content, inspect the checkpoint and outgoing payload. If it cannot quote prior
thinking text, that can still be expected even when the reasoning block was
stored and replayed, because provider-native reasoning items are not normal
assistant-visible text.

## Responses Mode

Responses mode is the default for OpenRouter.

Runtime construction is in `_create_openrouter_llm()` in
`Nymeria/nymeria/vendor/react_agent/providers.py`.

Nymeria sets:

```python
use_responses_api=True
output_version="responses/v1"
store=False
base_url="https://openrouter.ai/api/v1"
```

When reasoning is enabled and the model metadata does not prove it unsupported,
Nymeria sends:

```json
{
  "reasoning": {
    "summary": "auto",
    "effort": "medium"
  }
}
```

`effort` comes from `LLM_REASONING_EFFORT` / per-thread `reasoning_effort`, or
defaults to `medium`.

### Responses Request Shape

The outgoing request uses `input`, not `messages`:

```json
{
  "model": "anthropic/claude-haiku-4.5",
  "input": [
    {
      "type": "message",
      "role": "user",
      "content": "hello"
    },
    {
      "type": "reasoning",
      "id": "rs_...",
      "status": "completed",
      "summary": [
        {
          "type": "summary_text",
          "text": "Plaintext reasoning summary when the provider exposed one."
        }
      ]
    },
    {
      "type": "function_call",
      "id": "fc_...",
      "call_id": "call_...",
      "name": "memory_read",
      "arguments": "{\"scope\": \"thread\"}"
    },
    {
      "type": "function_call_output",
      "id": "fco_...",
      "call_id": "call_...",
      "output": "[empty]"
    },
    {
      "type": "message",
      "role": "assistant",
      "id": "msg_...",
      "status": "completed",
      "content": [
        {
          "type": "output_text",
          "text": "Visible assistant answer."
        }
      ]
    },
    {
      "type": "message",
      "role": "user",
      "content": "next user message"
    }
  ],
  "reasoning": {
    "summary": "auto",
    "effort": "high"
  },
  "store": false,
  "stream": true,
  "max_output_tokens": 8192
}
```

OpenRouter's docs say assistant role messages in Responses history require
`id` and `status`, and tool outputs require an `id`. Nymeria normalizes those
fields after LangChain builds the payload.

### Responses History Normalization

OpenRouter-specific normalization is in
`_normalize_openrouter_responses_payload()` in
`Nymeria/nymeria/vendor/react_agent/providers.py`.

It does four important things:

1. Removes `previous_response_id`.
2. Adds stable IDs to assistant `message`, `function_call`, and
   `function_call_output` items when missing.
3. Adds `status: "completed"` to assistant `message` items.
4. Strips leaked inline `<think>...</think>` answer text before replay.

Nymeria intentionally uses full-history replay instead of provider-side
continuation. That keeps compaction, thread deletion, prompt changes, tool
availability, and frontend history aligned with the checkpoint.

### Responses Reasoning Streams

OpenRouter Responses can produce reasoning through several event/content shapes:

| Shape | Where it appears | Nymeria handling |
| --- | --- | --- |
| `response.reasoning_text.delta` | OpenRouter streaming event observed with Claude via OpenRouter | Converted into a LangChain `reasoning` content block. |
| `response.reasoning.delta` | Documented legacy/unified OpenRouter event | Converted into a LangChain `reasoning` content block. |
| `response.content_part.delta` | Some docs/examples show text-like deltas | Routed through the OpenRouter fallback when LangChain skips it. |
| `{"type": "reasoning", "summary": [...]}` | Stored `AIMessage.content` item | Rehydrated as frontend `thinking`. |
| `{"type": "reasoning", "content": [{"type": "reasoning_text", ...}]}` | Observed final Responses item for Claude via OpenRouter | Rehydrated as frontend `thinking`. |

The OpenRouter-aware stream path is in `ChatOpenAIWithReasoning._stream()` and
`_astream()`. These methods route Responses mode through `_stream_responses()`
and `_astream_responses()` so OpenRouter-specific reasoning events are not lost
inside LangChain's default stream handling.

Frontend display is provider-agnostic. `Nymeria/nymeria/core/agent.py` turns
typed reasoning blocks into SSE `thinking` events; the desktop app renders those
in the thinking dropdown.

### Responses Tool Calls

Responses tool calls are not chat messages. They are top-level `input` / `output`
items:

```json
{
  "type": "function_call",
  "id": "fc_1",
  "call_id": "call_123",
  "name": "get_weather",
  "arguments": "{\"location\":\"Boston, MA\"}"
}
```

The tool result is replayed as:

```json
{
  "type": "function_call_output",
  "id": "fc_output_1",
  "call_id": "call_123",
  "output": "{\"temperature\":\"72F\",\"condition\":\"Sunny\"}"
}
```

`call_id` links the result to the original tool call. If the model loops,
complains about missing tool output, or OpenRouter rejects the request, inspect
the replayed `function_call` / `function_call_output` pair first.

### Responses Caveats

- OpenRouter Responses is beta. Event names and item shapes can change.
- Nymeria does not auto-fallback from Responses to Chat Completions. Provider
  errors should surface clearly so the user can switch API mode intentionally.
- Reasoning displayed as `thinking` is stored and replayed in provider-native
  shape, but the model may not treat prior reasoning items as ordinary readable
  context. Do not use "repeat your prior thoughts" as the only test of replay.
- If a provider leaks `<think>` markup as normal answer text, Nymeria strips it
  from live display, `/history`, and later replay. It is not converted into
  durable thinking.

## Chat Completions Mode

Set `openai_api_mode = "chat_completions"` globally or per-thread to use the
older OpenAI-compatible endpoint:

```text
POST https://openrouter.ai/api/v1/chat/completions
```

The request uses `messages`:

```json
{
  "model": "anthropic/claude-haiku-4.5",
  "messages": [
    {
      "role": "system",
      "content": "..."
    },
    {
      "role": "user",
      "content": "hello"
    },
    {
      "role": "assistant",
      "content": "Visible assistant answer.",
      "reasoning_details": [
        {
          "type": "reasoning.text",
          "text": "Provider-specific reasoning detail."
        }
      ]
    },
    {
      "role": "user",
      "content": "next user message"
    }
  ],
  "stream": true,
  "extra_body": {
    "reasoning": {
      "enabled": true,
      "effort": "high"
    }
  }
}
```

Nymeria only sends `extra_body.reasoning` when extended thinking is enabled and
model metadata does not say reasoning is unsupported.

### Chat Completions Reasoning

OpenRouter Chat Completions reasoning may arrive as:

- `choices[0].delta.reasoning`
- `choices[0].delta.reasoning_content`
- `choices[0].delta.reasoning_details`

The `ChatOpenAIWithReasoning` subclass normalizes these into LangChain chunk
metadata so `core/agent.py` can emit the same frontend `thinking` SSE events
used by Responses mode.

When OpenRouter returns `reasoning_details`, Nymeria stores a replayable version
on the `AIMessage`. On a later Chat Completions request, the subclass injects it
back into the assistant message as `reasoning_details`. If only plaintext
`reasoning` / `reasoning_content` exists, Nymeria can replay that as
`message.reasoning`.

### Chat Completions Tool Calls

Chat Completions uses the OpenAI-style message/tool shape:

- Assistant message has `tool_calls`.
- Tool result is a message with `role: "tool"` and `tool_call_id`.
- LangChain handles most serialization.

If tool replay breaks only in `chat_completions` mode, inspect the `messages`
array and `tool_call_id` continuity rather than Responses `input` items.

## Legacy Text Completions

OpenRouter also exposes a non-chat completions endpoint:

```text
POST https://openrouter.ai/api/v1/completions
```

Nymeria does not use this endpoint for agent turns. All OpenRouter traffic goes
through either Responses (`input`) or Chat Completions (`messages`) because the
agent requires multi-turn chat history, tool calls, and provider-specific
reasoning metadata.

If debugging shows `prompt` / `choices[].text` shapes instead of `messages` or
Responses `input`, that traffic is not coming from Nymeria's normal OpenRouter
provider path.

## Choosing A Mode

Use Responses mode when:

- You want Nymeria's default OpenRouter path.
- You are testing Responses reasoning streams.
- You want provider-native Responses tool/reasoning item shapes.
- The selected model works correctly on OpenRouter's beta endpoint.

Use Chat Completions mode when:

- OpenRouter's Responses beta rejects a model or request.
- A model behaves better through the older `/chat/completions` endpoint.
- You are debugging against older OpenRouter reasoning behavior
  (`delta.reasoning`, `reasoning_details`).

Do not add silent fallback. A fallback can hide provider regressions, create
different replay semantics between turns, and make debugging history impossible.

## Debugging Checklist

### 1. Confirm Thread Config

Use MCP:

```json
nymeria_get_thread_config({
  "thread_id": "<thread-id>"
})
```

Check:

- `provider` is `openrouter`.
- `model` is the intended OpenRouter slug.
- `openai_api_mode` is `responses` or `chat_completions`.
- `extended_thinking` and `reasoning_effort` are what you expect.

### 2. Confirm Frontend History Rehydration

Use MCP:

```json
nymeria_get_thread_history({
  "thread_id": "<thread-id>",
  "include_internal": false,
  "include_markdown": false,
  "limit": 20
})
```

For reasoning-capable turns, look for assistant `steps` like:

```json
[
  {
    "type": "thinking",
    "content": "..."
  },
  {
    "type": "response",
    "content": "..."
  }
]
```

If `/history` has `thinking` but the frontend does not show it, debug the
frontend/SSE rendering path. If `/history` does not have `thinking`, inspect the
checkpoint and provider stream.

### 3. Inspect The Raw Checkpoint

In Docker/Postgres deployments, run inside the API container so installed Python
deps and environment are available:

```bash
docker exec nymeria-api bash -lc 'python - <<'"'"'PY'"'"'
from nymeria.vendor.react_agent.graph import create_checkpointer
from nymeria.vendor.react_agent.config import CheckpointerConfig
from nymeria.config.settings import get_settings

thread_id = "<thread-id>"
settings = get_settings()
saver = create_checkpointer(CheckpointerConfig(
    backend="postgres",
    postgres_uri=settings.postgres_uri,
))
cp = saver.get_tuple({"configurable": {"thread_id": thread_id}})
messages = cp.checkpoint.get("channel_values", {}).get("messages", [])

for i, msg in enumerate(messages):
    print("---", i, type(msg).__name__, getattr(msg, "id", None))
    print("tool_calls", len(getattr(msg, "tool_calls", []) or []))
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                print(block.get("type"), sorted(block.keys()))
    else:
        print(type(content).__name__, str(content)[:120].replace("\n", "\\n"))
PY'
```

Expected Responses reasoning storage:

- `AIMessage.content` contains a dict with `type: "reasoning"`.
- Plaintext reasoning may be under `summary[].text`.
- Claude via OpenRouter may finalize plaintext reasoning under
  `content[].text` with `type: "reasoning_text"`.

### 4. Reconstruct The OpenRouter Payload

This shows what Nymeria would send from checkpoint messages. Use this for
payload-shape bugs, missing IDs, missing status fields, or missing reasoning.

```bash
docker exec nymeria-api bash -lc 'python - <<'"'"'PY'"'"'
from nymeria.vendor.react_agent.graph import create_checkpointer
from nymeria.vendor.react_agent.config import CheckpointerConfig, LLMConfig
from nymeria.vendor.react_agent.providers import create_llm
from nymeria.config.settings import get_settings

thread_id = "<thread-id>"
settings = get_settings()
saver = create_checkpointer(CheckpointerConfig(
    backend="postgres",
    postgres_uri=settings.postgres_uri,
))
cp = saver.get_tuple({"configurable": {"thread_id": thread_id}})
messages = cp.checkpoint.get("channel_values", {}).get("messages", [])

llm = create_llm(LLMConfig(
    provider="openrouter",
    model="anthropic/claude-haiku-4.5",
    api_key=settings.openrouter_api_key,
    base_url=None,
    temperature=None,
    extended_thinking=True,
    reasoning_effort="high",
    openai_api_mode="responses",
))

payload = llm._get_request_payload(messages, stream=True)
print("keys", sorted(payload.keys()))
print("has messages", "messages" in payload)
print("has input", "input" in payload)
print("store", payload.get("store"))
print("reasoning", payload.get("reasoning"))

for i, item in enumerate(payload.get("input") or payload.get("messages") or []):
    if isinstance(item, dict):
        print(i, item.get("type"), item.get("role"), item.get("id"), item.get("status"))
PY'
```

Expected Responses payload:

- Has `input`.
- Does not have `messages`.
- Has `store: false`.
- Does not have `previous_response_id`.
- Assistant output messages have `id` and `status: "completed"`.
- Tool outputs have `id`.
- Reasoning config is present when enabled and supported.

### 5. Check Live Stream Events

When thinking does not stream, distinguish these cases:

- Provider did not send reasoning events.
- Provider sent events LangChain dropped.
- Nymeria emitted `thinking`, but frontend did not render it.
- The model used hidden/encrypted reasoning only, with no plaintext field.

For Nymeria-level testing, prefer MCP:

```json
nymeria_chat({
  "thread_id": "<disposable-thread-id>",
  "message": "Compute 17 * 23. Think carefully internally, then answer with only the final number."
})
```

Then call `/history` through MCP and confirm a `thinking` step exists.

### 6. Useful Log Signals

API logs should show:

```text
[LLM] OpenRouter Responses API mode enabled for <model>; replaying full checkpointed history
```

For detailed message dumps, enable the `llm` log profile if needed. Avoid logging
full payloads by default because they can include user data, tool outputs, and
reasoning text.

## Common Symptoms

| Symptom | Likely area | What to inspect |
| --- | --- | --- |
| No thinking in frontend, but `/history` has thinking | Frontend rendering/SSE state | Desktop chat store and ThinkingBlock handling. |
| No thinking in `/history` | Provider stream or backend extraction | Raw stream events, `ChatOpenAIWithReasoning`, `core/agent.py` reasoning extraction. |
| Thinking streams live but disappears after reload | Checkpoint storage/history rehydration | `AIMessage.content` reasoning blocks and `get_conversation_history()`. |
| Model cannot quote prior thoughts | Usually provider/model behavior | Verify checkpoint and payload; do not assume reasoning items are readable text. |
| OpenRouter rejects Responses history | Payload normalization | Assistant `message.id`, `message.status`, `function_call_output.id`, item ordering. |
| Tool loop or missing tool output | Tool replay shape | `function_call.call_id` and matching `function_call_output.call_id`. |
| Responses model fails but Chat Completions works | OpenRouter beta/model compatibility | Switch API Mode explicitly to `chat_completions`. |
| `<think>` text appears in answer | Provider leaked reasoning as text | Nymeria should strip it; inspect inline thinking sanitizer paths. |

## Code Map

| File | Responsibility |
| --- | --- |
| `nymeria/vendor/react_agent/providers.py` | Builds OpenRouter `ChatOpenAI`, selects Responses vs Chat Completions, normalizes payloads, rescues OpenRouter reasoning stream events, replays chat-completions reasoning details. |
| `nymeria/core/agent.py` | Streams LangGraph events to Nymeria SSE events, extracts thinking/content blocks, strips inline `<think>` leaks, rehydrates `/history`. |
| `nymeria/vendor/react_agent/nodes.py` | Builds the message list passed to the LLM node and handles provider-specific sanitation before invocation. |
| `nymeria/config/settings.py` | Global `OPENAI_API_MODE`, provider, model, reasoning, and base URL settings. |
| `nymeria/core/thread_config.py` | Per-thread overrides for provider/model/API mode/reasoning. |
| `tests/test_openai_responses_config.py` | Unit coverage for OpenRouter Responses payload shape, normalization, and stream conversion. |
| `tests/test_reasoning_history.py` | Unit coverage for reasoning history rehydration and inline thinking stripping. |

## Testing Matrix

Run focused backend tests after changing provider code:

```bash
docker exec nymeria-api python -m pytest -q \
  /app/tests/test_reasoning_history.py \
  /app/tests/test_openai_responses_config.py
```

Run a live MCP smoke if `OPENROUTER_API_KEY` is configured:

1. Create or choose a disposable thread.
2. Set per-thread config to OpenRouter, `openai_api_mode: "responses"`,
   `extended_thinking: true`, and a reasoning model.
3. Send one turn that should produce thinking.
4. Confirm `nymeria_chat` returns a `thinking` step.
5. Confirm `nymeria_get_thread_history` rehydrates the same thinking step.
6. Send a second turn to confirm full-history replay still works.
7. Delete the disposable thread.

For Chat Completions compatibility, repeat with
`openai_api_mode: "chat_completions"` and confirm `thinking` still streams for a
model that emits OpenRouter chat-completions reasoning fields.

## Design Rules

- Keep OpenRouter special cases isolated in provider code and the reasoning
  extraction boundary. Do not spread OpenRouter conditionals through frontend
  components.
- Preserve full-history replay. Do not switch to `previous_response_id` without
  redesigning compaction, deletion, prompt changes, and checkpoint ownership.
- Do not add silent fallback from Responses to Chat Completions.
- Treat provider-leaked inline `<think>` text as malformed answer text. Strip it
  rather than storing it as thinking.
- When debugging, always separate these layers: provider stream, LangChain chunk,
  Nymeria SSE event, checkpoint storage, `/history` rehydration, frontend render.
