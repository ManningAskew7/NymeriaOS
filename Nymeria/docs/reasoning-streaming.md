# Reasoning / Thinking Token Streaming

How live-streaming reasoning tokens flow from the LLM provider to the frontend thinking dropdown, across three provider families (Anthropic native, OpenAI-compatible chat-completions, and the Responses API).

**Status:** working for Anthropic, OpenAI-compatible chat-completions providers, and opt-in OpenAI Responses API mode as of 2026-04-24. Responses mode is the recommended path for CLIProxy Codex OAuth threads that need native reasoning/tool-context continuation.

## TL;DR

- The **frontend** has one code path for the thinking dropdown: it listens for SSE events of `type: "thinking"` and streams the bytes into a live-collapsing block.
- The **backend agent** (`core/agent.py`) emits that event from three sources — Anthropic's typed `{"type": "thinking"}` content blocks, OpenAI-compatible `additional_kwargs["reasoning_content"]`, and Responses API `{"type": "reasoning"}` summary blocks.
- The **ChatOpenAI subclass** (`vendor/react_agent/providers.py`) intercepts the raw streaming chunk and rescues OpenAI-compatible reasoning fields that langchain-openai deliberately drops (`delta.reasoning_content`, `delta.reasoning`, and `delta.reasoning_details`).
- The **history endpoint** rehydrates all supported storage shapes into the same frontend `steps` format: Anthropic thinking from `AIMessage.content` typed blocks, OpenAI-compatible reasoning from `AIMessage.additional_kwargs["reasoning_content"]` / `["reasoning_details"]`, and Responses reasoning summaries from `AIMessage.content` blocks.
- The **agent node** strips malformed Anthropic replay blocks shaped like `{"type": "thinking", "signature": "..."}` before the next provider call. Anthropic requires either a `thinking` or `redacted_thinking` field on replay, and summarized thinking can leave a signature-only block in checkpoint history.
- For **CLIProxy's GPT-5.5 sidecar**, the proxy itself already does the hard translation: it turns upstream Codex Responses-API `response.reasoning_summary_text.delta` events into `choices[0].delta.reasoning_content` strings on the chat-completions wire. We just have to not drop them client-side.
- For **native OpenAI Responses mode**, Nymeria replays the checkpointed Responses content blocks on each request instead of using `previous_response_id`. That keeps LangGraph's checkpoint authoritative for compaction, system prompt changes, injected context, and per-thread tool changes while still preserving prior reasoning/tool blocks in provider-native shape.

If CLIProxy gets wiped, the only thing that needs rebuilding is the sidecar container and its auth tokens (see [docs/cliproxy.md](cliproxy.md)). The Python/Svelte code in this repository requires no change — the fix is committed and automatic for any OpenAI-compatible endpoint that emits reasoning in either convention.

## Wire formats by provider

### Anthropic (`/v1/messages`)

Native extended-thinking path. `langchain-anthropic` parses Anthropic's typed content blocks and exposes them as a list on `AIMessageChunk.content`:

```python
chunk.content = [
    {"type": "thinking", "thinking": "The user is asking..."},
    {"type": "text", "text": "The answer is 42."},
]
```

Matched by `core/agent.py` at the `on_chat_model_stream` branch: `block_type == "thinking"` → yield `{"type": "thinking", ...}`.

Before replaying checkpointed history to Anthropic, `vendor/react_agent/nodes.py` removes only invalid signature-only thinking blocks: `{"type": "thinking", "signature": "..."}`. Those blocks can appear after summarized thinking, but Anthropic rejects them on the next request with `messages.N.content.0.thinking.thinking: Field required`. Blocks that include `thinking` (including an empty string) or `redacted_thinking` are preserved.

### OpenAI-compatible chat-completions (`/v1/chat/completions`)

This is what CLIProxy's GPT-5.5 sidecar, OpenRouter, DeepSeek, and Qwen-via-OpenRouter expose. Two different key conventions coexist on the wire:

| Key | Emitted by | Shape |
| --- | --- | --- |
| `delta.reasoning_content` | CLIProxy Codex sidecar, DeepSeek, Qwen | plaintext string deltas, one per token |
| `delta.reasoning` | OpenRouter's unified wrapper | plaintext string deltas, one per token |
| `delta.reasoning_details` | OpenRouter's structured reasoning wrapper | typed reasoning detail objects such as `reasoning.text`, `reasoning.summary`, or encrypted blocks |

`langchain-openai` documents that it **intentionally does not** extract these provider-specific fields (see the docstring at `langchain_openai/chat_models/base.py:8` and the explicit comment at `:576`). It tells you to subclass for provider-specific behaviour — which we do.

### Responses API (`/v1/responses`)

The newer OpenAI endpoint used automatically by langchain-openai for models whose name starts with `gpt-5-pro`, `gpt-5.2-pro`, `gpt-5.4-pro`, or contains `codex` (see `_model_prefers_responses_api` in `langchain_openai/chat_models/base.py:559`). Nymeria now defaults `provider=openai` to this path with `openai_api_mode="responses"` for OpenAI-compatible endpoints such as the CLIProxy Codex OAuth sidecar.

Reasoning arrives as typed blocks with `type: "reasoning"`. Nymeria streams and rehydrates plaintext `summary` text as frontend thinking. Raw encrypted reasoning (`encrypted_content`) is preserved in the LangGraph `AIMessage` if the provider returns it, but is never displayed or logged. On later turns, Nymeria sends the checkpointed Responses items back through `input` rather than `previous_response_id`, so compaction and prompt/context rewrites are respected by the next model call.

## End-to-end pipeline (OpenAI-compatible path)

```
┌─────────────────────┐
│ Upstream (Codex /    │  response.reasoning_summary_text.delta
│ DeepSeek / Qwen /    │  + response.output_text.delta
│ OpenAI reasoning)    │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────────┐
│ CLIProxy translator     │  Codex-sidecar only. For direct OpenRouter/
│ codex_openai_response.go│  OpenAI calls this stage is bypassed.
│                         │  Maps:
│                         │    reasoning_summary_text.delta
│                         │    → choices[0].delta.reasoning_content
│                         │    reasoning_summary_text.done
│                         │    → choices[0].delta.reasoning_content = "\n\n"
└──────────┬──────────────┘
           │  OpenAI-compatible SSE
           ▼
┌─────────────────────┐
│ OpenAI Python SDK   │  Parses SSE into ChatCompletionChunk objects.
│ (openai)            │  Preserves reasoning_content in delta dict.
└──────────┬──────────┘
           │
           ▼
┌─────────────────────────────────┐
│ langchain-openai ChatOpenAI     │  Calls chunk.model_dump() → dict with
│ _convert_chunk_to_generation_   │  choices[0].delta.reasoning_content
│ chunk()                         │  still present. Standard _convert_delta_
│                                 │  to_message_chunk() then DROPS it.
└──────────┬──────────────────────┘
           │
           ▼
┌───────────────────────────────────────┐
│ ChatOpenAIWithReasoning (our subclass) │  Override point. Reads the raw
│ vendor/react_agent/providers.py        │  chunk BEFORE returning it and
│                                        │  stashes plaintext reasoning into
│                                        │  ["reasoning_content"] and structured
│                                        │  OpenRouter details into
│                                        │  ["reasoning_details"]
└──────────┬─────────────────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ core/agent.py               │  on_chat_model_stream handler checks
│ on_chat_model_stream        │  chunk.additional_kwargs["reasoning_content"]
│                             │  first → yields {"type": "thinking",
│                             │  "content": reasoning}
└──────────┬──────────────────┘
           │  SSE event bus
           ▼
┌─────────────────────────────┐
│ triggers/api.py /chat       │  Serialises {type, content} to an SSE line
│                             │  like `event: thinking\ndata: {...}`
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ Desktop: api.svelte.ts      │  Parses SSE, dispatches to chat store.
│ + chat store + ThinkingBlock│  ThinkingBlock renders live, collapses
│ component                   │  when the final answer starts.
└─────────────────────────────┘
```

For **Anthropic** the two middle rows collapse into a single typed-block branch inside the same `on_chat_model_stream` handler — both paths converge on the same `yield {"type": "thinking", ...}` line, so downstream rendering is identical.

## Why a subclass, not a monkey-patch

`langchain_openai/chat_models/base.py` explicitly recommends subclassing as the provider-specific extension mechanism (`"Use a provider-specific subclass for full provider support."`). The key override point is `_convert_chunk_to_generation_chunk`, which receives the full raw chunk dict (not just the delta). We call `super()` first to get the standard `AIMessageChunk`, then inject reasoning into its `additional_kwargs` before returning. That keeps every other langchain behaviour (tool-call deltas, usage metadata, finish reason propagation, response_metadata output versioning) intact.

We avoid monkey-patching because:
- `_convert_delta_to_message_chunk` is a module-level free function, not a method — any patch would collide with other `ChatOpenAI` consumers sharing the process (plus it's called from both sync and async paths).
- The existing `_patch_langchain_anthropic_proxy_compat` monkey-patch in the same file demonstrates how awkward upstream patching is. A subclass is contained to the creator function.

## Code map

All under `/opt/NymeriaOS/Nymeria/`:

| Where | What |
| --- | --- |
| `nymeria/vendor/react_agent/providers.py`, `_get_chat_openai_with_reasoning()` | Factory that builds `ChatOpenAIWithReasoning`, a `ChatOpenAI` subclass overriding `_convert_chunk_to_generation_chunk`. Normalises `delta.reasoning_content` (CLIProxy / DeepSeek / Qwen), `delta.reasoning` (OpenRouter), and displayable `delta.reasoning_details` text into `message.additional_kwargs["reasoning_content"]`. It also preserves raw OpenRouter `reasoning_details` in `message.additional_kwargs["reasoning_details"]` and replays them on later OpenRouter chat-completions requests as assistant-message `reasoning_details`. |
| `nymeria/vendor/react_agent/providers.py`, `_create_openai_llm` | Uses `ChatOpenAIWithReasoning` instead of vanilla `ChatOpenAI`. Covers direct OpenAI, the CLIProxy GPT-5.5 sidecar, and any OpenAI-compatible endpoint that sets `provider=openai`. CLIProxy-looking OpenAI base URLs are normalized to include `/v1` before `ChatOpenAI` is constructed; without that, Responses mode hits `POST /responses` and returns `404 page not found`. When `llm_config.openai_api_mode="responses"`, it sets `use_responses_api=True`, `output_version="responses/v1"`, and `store=False`; it intentionally does not set `use_previous_response_id`, so the next request replays checkpointed Responses items without relying on provider-side response retention. |
| `nymeria/vendor/react_agent/providers.py`, `_create_openrouter_llm` | Same subclass, used for all OpenRouter traffic. Picks up reasoning when the model supports it and `extended_thinking=True` is set on the thread. |
| `nymeria/vendor/react_agent/nodes.py`, `_sanitize_messages_for_anthropic()` | Replay-time guard for Anthropic history. Drops malformed signature-only `thinking` blocks before calling the provider while leaving stored checkpoints unchanged. |
| `nymeria/core/agent.py`, `on_chat_model_stream` branch | First checks `chunk.additional_kwargs.get("reasoning_content")` and yields a `thinking` SSE event. Then handles typed `thinking` and `reasoning` content blocks with a per-LLM-call dedupe guard. Plain answer blocks of type `text` or `output_text`, plus provider-emitted bare string blocks, stream as `response` so pre-tool commentary stays visible. |
| `nymeria/core/agent.py`, `get_conversation_history()` | Rehydrates saved reasoning into history `steps`. Anthropic thinking is read from typed content blocks; OpenAI-compatible reasoning is read from `AIMessage.additional_kwargs["reasoning_content"]`, `["reasoning_details"]`, or legacy `"reasoning"`; Responses summaries are read from `AIMessage.content` reasoning blocks. |
| `nymeria/triggers/api.py`, `/chat` SSE handler | Serialises `{"type": "thinking", "content": ...}` into the on-the-wire SSE line the frontend consumes. No special-casing per provider. |
| `/opt/NymeriaOS/nymeria-desktop/src/lib/services/api.svelte.ts` | Parses SSE, dispatches `thinking` events to the chat store. |
| `/opt/NymeriaOS/nymeria-desktop/src/lib/components/chat/ThinkingBlock.svelte` (or equivalent) | Renders the live-collapsing dropdown. Provider-agnostic. |
| `/opt/NymeriaOS/CLIProxyAPI-main/internal/translator/codex/openai/chat-completions/codex_openai_response.go` (upstream, not our code) | The CLIProxy-side translation. Line 105 maps `response.reasoning_summary_text.delta` → `choices[0].delta.reasoning_content`. Line 112 emits `\n\n` as a section separator on `reasoning_summary_text.done`. |

## Provider compatibility matrix

| Provider / model | Endpoint langchain uses | Wire format | Works with our code? |
| --- | --- | --- | --- |
| Anthropic direct | `/v1/messages` | typed blocks in `content` | ✅ yes (existing Anthropic path) |
| Anthropic via pinned CLIProxy | `/v1/messages` | typed blocks in `content` | ✅ yes |
| CLIProxy GPT-5.5 sidecar with `openai_api_mode="chat_completions"` | `/v1/chat/completions` | `delta.reasoning_content` (plaintext summary, translated from upstream) | ✅ yes, via subclass; not recommended if thinking is enabled |
| CLIProxy GPT-5.5 sidecar with default `openai_api_mode="responses"` | `/v1/responses` | typed `reasoning` summary blocks + `resp_*` id | ✅ yes, with checkpoint replay |
| OpenRouter DeepSeek-R1 / Qwen thinking / Claude-via-OR with `reasoning.enabled=true` | `/chat/completions` | `delta.reasoning` and/or `delta.reasoning_details` | ✅ yes, via subclass; prior OpenRouter assistant reasoning is replayed with `message.reasoning_details` when available |
| Pure OpenAI reasoning models via `openai.com` with default `openai_api_mode="responses"` | `/v1/responses` | Typed reasoning blocks; summary optional and often empty | ✅ surfaces plaintext summaries when present |
| gpt-5-pro family, gpt-5.2-pro, gpt-5.4-pro, any `*codex*` name — direct (not via CLIProxy) | `/v1/responses` (auto-switched by langchain-openai `_model_prefers_responses_api`) | Typed reasoning blocks; summary optional and often empty; raw reasoning is encrypted | ✅ surfaces plaintext summaries when present |

The CLIProxy sidecar does not trigger langchain-openai's built-in Responses auto-switch for `gpt-5.5` because the model name does not match any prefix in `_RESPONSES_API_ONLY_PREFIXES` (`gpt-5-pro`, `gpt-5.2-pro`, `gpt-5.4-pro`) and does not contain `codex`. Nymeria's default `openai_api_mode="responses"` forces the correct endpoint anyway; `chat_completions` remains available as a compatibility override.

## Sidecar requirements for reasoning to keep working

If the GPT-5.5 CLIProxy sidecar gets wiped and you're rebuilding from scratch, the **reasoning-translation** behaviour requires the following to line up. All of them are already satisfied by the procedure in [docs/cliproxy.md § Codex OAuth GPT-5.5 sidecar](cliproxy.md#codex-oauth-gpt-55-sidecar), but it's worth stating them explicitly because they're the failure points:

1. **Image version ≥ v6.9.36.** The Codex→chat-completions reasoning translator at `internal/translator/codex/openai/chat-completions/codex_openai_response.go:105` was added in that release. Older images (including the pinned `v6.9.0` Claude proxy) do not have it and will strip reasoning before it reaches Nymeria. Use `eceasy/cli-proxy-api:latest` for the sidecar — it bumps as new Codex models land.
2. **`config.yaml` has a non-empty `api-keys:` list.** CLIProxy's local gatekeeper. The value isn't upstream auth — it's what you paste into the thread's "API Key" field. Default in our repo is `cpx-latest-local-test` (see `CLIProxyAPI-main/config.nymeria.example.yaml`).
3. **A valid Codex OAuth token in `auths/`.** Generated via the `-codex-device-login -no-browser` flow (see cliproxy.md). This is the subscription-side auth. Upstream reasoning visibility depends on this account's model access — Plus-tier gives `gpt-5.5` with reasoning summaries; free tier currently does not.
4. **The sidecar container is attached to the `nymeria_nymeria-network` Docker network.** `docker network connect nymeria_nymeria-network cli-proxy-api-latest`. Without this, the Nymeria API container resolves `http://cli-proxy-api-latest:8317` → no route. Per-thread `base_url` would have to switch to a host-routable address (`http://host.docker.internal:8318/v1`) as a fallback.

Given those four, reasoning streaming works immediately — no Python or Svelte change is ever needed after a sidecar rebuild.

## Debugging

### Is CLIProxy emitting reasoning at all?

```bash
curl -s -N -X POST http://localhost:8318/v1/chat/completions \
  -H "Authorization: Bearer cpx-latest-local-test" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.5","stream":true,"reasoning_effort":"high",
       "messages":[{"role":"user","content":"Think step by step: what is 17*23?"}]}' \
  2>/dev/null | grep -o '"reasoning_content":"[^"]*"' | head -10
```

Expected: ten `"reasoning_content":"..."` lines. If you get none:
- Try `reasoning_effort: "medium"` or `"high"` — low-effort often skips reasoning entirely on trivial prompts.
- Check `docker logs cli-proxy-api-latest --tail 60` for `Suspended client ... quota` or `model_cooldown` lines. If present, `docker restart cli-proxy-api-latest` clears in-memory suspension, then retry.
- Check the auth token is for a Plus-tier Codex account. Free-tier Codex OAuth tokens do not receive reasoning summaries.

### Is the subclass capturing reasoning?

```bash
docker exec -i nymeria-api python - <<'PY'
from nymeria.vendor.react_agent.providers import _get_chat_openai_with_reasoning
Cls = _get_chat_openai_with_reasoning()
llm = Cls(
    model='gpt-5.5',
    api_key='cpx-latest-local-test',
    base_url='http://cli-proxy-api-latest:8317/v1',
    model_kwargs={'reasoning_effort': 'high'},
)
rc = []
for chunk in llm.stream('Think hard: what is 17*23?'):
    r = (chunk.additional_kwargs or {}).get('reasoning_content')
    if r:
        rc.append(r)
print('reasoning chunks:', len(rc))
print('preview:', ''.join(rc)[:200])
PY
```

Expected: 20+ chunks and a plaintext preview. If `reasoning chunks: 0` but the curl above shows the wire has them, the subclass isn't being used — check `_create_openai_llm` and `_create_openrouter_llm` in providers.py both call `_get_chat_openai_with_reasoning()`.

### Is the agent emitting thinking SSE events?

Send a chat with `curl -N http://localhost:8000/chat ... --data-raw '{...}'` to a thread configured for the sidecar, then `grep -E '^event: thinking|^data:'` the stream. You should see `event: thinking` lines interleaved with `event: response` lines.

### Is the frontend rendering them?

Open devtools → Network → the `/chat` EventSource. Confirm `event: thinking` lines are arriving. If they are but the dropdown isn't appearing, the regression is in the frontend (chat store or ThinkingBlock), not the backend.

### Is history rehydrating saved reasoning?

Load the thread history and inspect assistant `steps`:

```bash
# $NYMERIA_TOKEN is your per-user account token (`nym_…`); see docs/accounts.md.
curl -s http://localhost:8000/threads/<thread_id>/history \
  -H "Authorization: Bearer $NYMERIA_TOKEN" \
  | jq '.messages[] | select(.role=="assistant") | {content_len:(.content|length), steps}'
```

Expected for a GPT-5.5 sidecar turn: a `steps` array with a `thinking` entry before the final `response` entry. If live streaming showed thinking but history has no `thinking` step, check whether the saved `AIMessage` has `additional_kwargs.reasoning_content`.

## Known limitations

- **Responses summary availability.** The raw reasoning body is encrypted for privacy. Nymeria only displays plaintext summary text, and some Codex OAuth responses may return empty summaries even though checkpoint replay can still preserve provider-native reasoning/tool blocks.
- **Provider-specific reasoning continuation outside OpenAI Responses.** OpenRouter chat-completions reasoning replay is implemented for `message.reasoning_details` / `message.reasoning`. Other OpenAI-compatible servers may stream/display reasoning through `reasoning_content`, but Nymeria does not add those nonstandard fields back into later requests unless the base URL is OpenRouter.

## Related docs

- [cliproxy.md](cliproxy.md) — full sidecar setup and OAuth procedure. Required reading if rebuilding from scratch.
- [architecture.md](architecture.md) — overall SSE event protocol and streaming model.
- [reasoning-tokens-investigation.md](reasoning-tokens-investigation.md) — the 2026-02-08 investigation that identified where langchain-openai was dropping reasoning, superseded by this doc.
- [configuration.md](configuration.md) — per-thread LLM config fields (`provider`, `base_url`, `api_key`, `reasoning_effort`, `extended_thinking`).
