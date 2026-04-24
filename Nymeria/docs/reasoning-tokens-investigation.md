# Reasoning Token Streaming: Investigation & Findings

> **STATUS: RESOLVED (2026-04-24).** The drop described below has been fixed. See [reasoning-streaming.md](reasoning-streaming.md) for the current implementation, pipeline diagram, and debugging recipes. This document is retained as a historical record of the investigation.
>
> Summary of the fix: a thin `ChatOpenAIWithReasoning` subclass in `nymeria/vendor/react_agent/providers.py` overrides `_convert_chunk_to_generation_chunk` to rescue both `delta.reasoning_content` (CLIProxy Codex, DeepSeek, Qwen) and `delta.reasoning` (OpenRouter) into `additional_kwargs["reasoning_content"]`. The agent handler in `core/agent.py` then emits a `thinking` SSE event, which the existing frontend dropdown renders. No frontend change was required.

> **Date:** 2026-02-08
> **Original status:** Documented for future reference — no code changes made
> **Context:** Extended Thinking enabled via OpenRouter, tested with `anthropic/claude-sonnet-4.5` and `qwen/qwen3-coder-next`

## Summary

When Extended Thinking is enabled, OpenRouter **does** stream reasoning tokens back to us. However, LangChain's `ChatOpenAI` silently drops them during the Chat Completions streaming path. The reasoning text never reaches Nymeria's streaming handler in `agent.py`.

Currently this is mostly fine — reasoning works (the model thinks before answering), but the streamed reasoning text from OpenRouter chat completions is not surfaced in Nymeria's UI. This document captures the full pipeline analysis for if we want to surface it later.

## The Streaming Pipeline

```
OpenRouter API  →  OpenAI Python SDK  →  LangChain ChatOpenAI  →  Nymeria agent.py  →  SSE to Frontend
                                              ↑
                                    reasoning dropped HERE
```

## What OpenRouter Actually Sends

Confirmed via raw OpenAI SDK test (bypassing LangChain):

```python
from openai import OpenAI
client = OpenAI(api_key=..., base_url='https://openrouter.ai/api/v1')
stream = client.chat.completions.create(
    model='anthropic/claude-sonnet-4.5',
    messages=[{'role': 'user', 'content': 'What is 2+2? One word answer.'}],
    stream=True,
    extra_body={'reasoning': {'enabled': True, 'effort': 'high'}}
)
```

Each streaming chunk's delta contains:

```json
// Reasoning phase (content is empty, reasoning has the thinking text):
{"content": "", "role": "assistant", "reasoning": "The question is asking what 2+2 equals..."}

// Answer phase (content has the final answer, no more reasoning):
{"content": "Four", "role": "assistant", "reasoning_details": []}
```

**Fields sent by OpenRouter:**
- `content` — The final answer text (empty during reasoning phase)
- `reasoning` — The thinking text (only during reasoning phase)
- `reasoning_details` — Array of structured reasoning blocks with `type`, `text`, `signature`, `format`, `index`

## Where LangChain Drops Reasoning

**File:** `.venv/lib/python3.13/site-packages/langchain_openai/chat_models/base.py`

**Function:** `_convert_delta_to_message_chunk()` (line 368)

```python
def _convert_delta_to_message_chunk(
    _dict: Mapping[str, Any], default_class: type[BaseMessageChunk]
) -> BaseMessageChunk:
    content = cast(str, _dict.get("content") or "")  # ← LINE 374: Only reads "content"
    additional_kwargs: dict = {}
    # ... only checks for function_call and tool_calls ...
    # NEVER reads _dict.get("reasoning") or _dict.get("reasoning_details")
```

The delta dict has `reasoning` and `reasoning_details` keys, but this function only extracts `content`, `function_call`, and `tool_calls`. Everything else is silently discarded.

**Result in Nymeria's agent.py streaming handler:**
- `chunk.content` = `""` (empty string) during reasoning phase
- `chunk.additional_kwargs` = `{}` (empty)
- No `reasoning_content` attribute exists on the chunk
- The reasoning text is completely gone

## Why `model_kwargs["reasoning"]` Caused `[object Object]`

LangChain v1.1.7 has `reasoning` as a first-class field on `ChatOpenAI` (line 638). When `reasoning` appeared in `model_kwargs`, LangChain's routing logic detected it:

**Function:** `_use_responses_api()` (line 3715)

```python
def _use_responses_api(payload: dict) -> bool:
    responses_only_args = {
        "include", "previous_response_id",
        "reasoning",  # ← THIS triggers Responses API
        "text", "truncation",
    }
    return bool(uses_builtin_tools or responses_only_args.intersection(payload))
```

**Line 3017-3018:**
```python
def _stream(self, *args, **kwargs):
    if self._use_responses_api({**kwargs, **self.model_kwargs}):  # ← model_kwargs checked here
        return super()._stream_responses(*args, **kwargs)  # Responses API path
    return super()._stream(*args, **kwargs)                # Chat Completions path
```

So `model_kwargs["reasoning"]` → LangChain detects `"reasoning"` in payload → switches to Responses API → calls `client.responses.create()` instead of `client.chat.completions.create()` → completely different request format (`input` instead of `messages`) → OpenRouter doesn't support Responses API → broken response → `[object Object]` in chat.

## Why `extra_body` Fixes It

`extra_body` is passed directly to the OpenAI Python SDK's HTTP request body. It is **not** merged into the kwargs/payload that LangChain inspects for API routing. So:

1. `_use_responses_api()` sees no `"reasoning"` in the payload → returns `False`
2. LangChain stays on Chat Completions API → `client.chat.completions.create()`
3. The `extra_body={"reasoning": {...}}` is correctly passed through to OpenRouter
4. OpenRouter processes it and sends reasoning tokens in the delta
5. LangChain drops the reasoning tokens (but the model's final answer comes through fine)

## Current Behavior (Working)

| Scenario | What Happens |
|----------|-------------|
| Extended Thinking OFF | No OpenRouter reasoning payload is sent when extended thinking is disabled, so the model responds normally without streamed reasoning |
| Extended Thinking ON | `extra_body={"reasoning": {"enabled": true}}` is sent for compatible OpenRouter models, the model reasons internally, then streams the final answer |
| Extended Thinking ON + effort | `extra_body={"reasoning": {"enabled": true, "effort": "high"}}` — same as above, with a potentially longer reasoning phase |

The empty `content=''` chunks during reasoning phase are the model's reasoning tokens with the text stripped by LangChain. The pause is real thinking time.

## Future: How to Surface Reasoning Text

If we want to display reasoning text in the UI, there are several approaches:

### Option A: Patch `_convert_delta_to_message_chunk` (Monkey-patch)

Override LangChain's function to also extract `reasoning`:

```python
# In providers.py or agent.py startup
from langchain_openai.chat_models import base as _base

_original_convert = _base._convert_delta_to_message_chunk

def _patched_convert(_dict, default_class):
    chunk = _original_convert(_dict, default_class)
    reasoning = _dict.get("reasoning")
    if reasoning and hasattr(chunk, 'additional_kwargs'):
        chunk.additional_kwargs["reasoning"] = reasoning
    return chunk

_base._convert_delta_to_message_chunk = _patched_convert
```

Then in `agent.py`'s streaming handler, check `chunk.additional_kwargs.get("reasoning")`.

**Pros:** Minimal code, works with existing LangChain version
**Cons:** Fragile — breaks if LangChain updates the function signature

### Option B: Use OpenAI SDK directly for streaming (bypass LangChain streaming)

Use `ChatOpenAI` for non-streaming and a raw `openai.AsyncOpenAI` client for streaming, parsing deltas ourselves.

**Pros:** Full control over all fields
**Cons:** Significant refactor of `agent.py` streaming, lose LangGraph event stream integration

### Option C: Custom `ChatOpenAI` subclass

Override `_convert_chunk_to_generation_chunk` to intercept the raw chunk dict before it reaches `_convert_delta_to_message_chunk`:

```python
class NymeriaChatOpenAI(ChatOpenAI):
    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        # Extract reasoning from raw choice delta before LangChain drops it
        choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
        reasoning = None
        if choices and choices[0].get("delta"):
            reasoning = choices[0]["delta"].get("reasoning")

        gen_chunk = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        if gen_chunk and reasoning:
            gen_chunk.message.additional_kwargs["reasoning"] = reasoning
        return gen_chunk
```

**Pros:** Clean, inherits all other ChatOpenAI behavior, no monkey-patching
**Cons:** Need to use this subclass in `providers.py` instead of `ChatOpenAI`

### Option D: Wait for LangChain update

LangChain may add native support for OpenRouter's `reasoning` field in a future version, since reasoning models are becoming standard.

**Pros:** Zero maintenance
**Cons:** Unknown timeline

### Recommended Approach

**Option C** (custom subclass) is the cleanest path. It would require:
1. Creating `NymeriaChatOpenAI` in `providers.py` (or a new file)
2. Using it instead of `ChatOpenAI` in `_create_openrouter_llm()`
3. Updating `agent.py`'s stream handler to check `chunk.additional_kwargs.get("reasoning")`
4. Yielding reasoning as `{"type": "thinking", "content": reasoning_text, "is_reasoning": True}`
5. Frontend `MessageBubble.svelte` already handles thinking steps — just need to mark them as reasoning

## Current Codebase Notes

- Nymeria now stores `llm_extended_thinking` and `llm_reasoning_effort` in settings and exposes them through `/settings` and `/settings/llm/runtime`.
- Anthropic-native thinking blocks already flow through Nymeria's `thinking` SSE/UI pipeline; this investigation is specifically about OpenRouter chat-completions reasoning fields being dropped before they reach that pipeline.
- The `consult` tool separately uses OpenRouter reasoning and can read `reasoning_details` from non-streaming responses.

## Package Versions (at time of investigation)

```
langchain-openai==1.1.7
openai==2.17.0
langchain-core==1.2.9
```

## Related Files

| File | Relevance |
|------|-----------|
| `nymeria/vendor/react_agent/providers.py` | Where `extra_body` is set for OpenRouter |
| `nymeria/core/agent.py` (lines 1976-1986) | Streaming handler that processes `on_chat_model_stream` events |
| `.venv/.../langchain_openai/chat_models/base.py:374` | Where LangChain drops reasoning (line 374) |
| `.venv/.../langchain_openai/chat_models/base.py:3715` | Where LangChain decides Chat Completions vs Responses API |
