# Local LLM Integration Guide

How to run Nymeria with a local LLM via llama.cpp on consumer AMD (or NVIDIA) hardware, what works, what doesn't, and what to watch for.

## Quick Start

### Requirements

- **GPU**: any modern discrete GPU with Vulkan support. Tested on AMD RX 9070 XT (RDNA 4, 16GB VRAM). NVIDIA cards work via CUDA builds of llama.cpp.
- **llama.cpp**: prebuilt Windows-Vulkan release from `https://github.com/ggml-org/llama.cpp/releases`  -  pick the `llama-*-bin-win-vulkan-x64.zip` asset for AMD, or the CUDA variant for NVIDIA. Extract to any directory (e.g. `C:\llama.cpp\`).
- **Model**: a GGUF file. Recommended starting point: **Qwen 3 14B Q4_K_M from bartowski** (`bartowski/Qwen_Qwen3-14B-GGUF`). ~8.4 GB download, ~11 GB VRAM resident with 16K context.
- **Docker Desktop** running with the Nymeria stack up.

### Launch llama-server

```bash
llama-server.exe \
  -m /path/to/model.gguf \
  --host 0.0.0.0 \
  --port 8080 \
  -ngl 99 \
  --jinja \
  -fa on \
  --reasoning-format deepseek \
  -c 16384 \
  --alias local-llm
```

Key flags:
- `-ngl 99`  -  offload all layers to GPU
- `--jinja`  -  use the GGUF's embedded Jinja chat template (required for tool calling)
- `-fa on`  -  enable flash attention
- `--reasoning-format deepseek`  -  surface `<think>` blocks as `reasoning_content` (Qwen 3 is a hybrid reasoning model)
- `-c 16384`  -  context window (adjust based on available VRAM)
- `--alias local-llm`  -  the model name that appears in API responses

### Configure Nymeria

Edit `Nymeria/.env.docker` (working tree only  -  do NOT commit instance-specific values):

```env
LLM_PROVIDER=openai
LLM_BASE_URL=http://host.docker.internal:8080/v1
LLM_MODEL=local-llm
# Optional when auto-detection is wrong or unavailable:
# LLM_CONTEXT_LENGTH=16384
# LLM_OLLAMA_NUM_CTX=16384
```

The `host.docker.internal` hostname lets the Nymeria containers (api, worker, mcp) reach the llama-server running on the Windows/Mac host. This works because `docker-compose.yml` includes `extra_hosts: ["host.docker.internal:host-gateway"]` on all three services.

Restart the api container to pick up the change:
```bash
docker restart nymeria-api
```

### Or use the frontend

Settings → LLM tab → Provider: **Local LLM (OpenAI-compatible)** → enter `local-llm` as the model name → the API Base URL auto-populates to `http://host.docker.internal:8080/v1` → Save.

Per-thread overrides also work: Thread Settings → Model tab → Provider: Local LLM.

---

## Supported Local Endpoint Shapes

Nymeria treats loopback, `host.docker.internal`, container-local hostnames, private LAN IPs, link-local IPs, and Tailscale `100.64.0.0/10` addresses as local inference endpoints. Local OpenAI-compatible endpoints do not need an API key. When `LLM_PROVIDER=openai` points at one of those URLs and no OpenAI key is configured, Nymeria sends the placeholder key `not-needed`.

Common local engines:

| Engine | Base URL | Metadata Nymeria checks |
|--------|----------|-------------------------|
| Ollama | `http://host.docker.internal:11434/v1` | `/api/tags` for detection and `/api/show` for `num_ctx` or GGUF `context_length` |
| llama.cpp | `http://host.docker.internal:8080/v1` | `/v1/props` or `/props` for `default_generation_settings.n_ctx` |
| LM Studio | `http://host.docker.internal:1234/v1` | `/api/v1/models` for loaded-instance `context_length` |
| vLLM or other OpenAI-compatible servers | provider URL ending in `/v1` | `/v1/models/{model}` and `/v1/models` for `max_model_len`, `context_length`, or `max_tokens` |

`LLM_CONTEXT_LENGTH` overrides the detected context window for compaction and frontend context accounting. `LLM_OLLAMA_NUM_CTX` is Ollama-specific and is sent on each request as `extra_body.options.num_ctx`. Use it when the loaded Ollama runtime context should be smaller than the model's GGUF training maximum.

Per-thread model settings expose the same controls as `context_length` and `ollama_num_ctx`, so one experimentation thread can run a smaller local window while the global default stays unchanged.

### Ollama: native vs OpenAI-compat

Nymeria registers Ollama under two provider IDs that route through different paths. Pick the one that matches your use case:

| Provider ID | Wire shape | Adapter | When to use |
|-------------|-----------|---------|-------------|
| `ollama` | OpenAI Chat Completions at `:11434/v1/chat/completions` | `ChatOpenAIWithReasoning` | You want parity with the rest of Nymeria's OpenAI-compatible stack (CLIProxy, OpenRouter, etc.) and your model does not emit `<think>` blocks. |
| `ollama-native` | Ollama native protocol at `:11434/api/chat` | `langchain-ollama` `ChatOllama` | You want native reasoning round-trip on `qwen3`, `deepseek-r1`, `gpt-oss`, or any other Ollama build that surfaces `<think>` content. Sets the `reasoning` flag automatically when extended thinking is enabled. |

`langchain-ollama` covers Ollama's native protocol only. The OpenAI-compat shim that Ollama also exposes at `/v1/chat/completions` continues to route through `_create_openai_compatible_llm` under the legacy `ollama` provider id, so existing thread configs keep working unchanged.

Existing per-thread `provider="ollama"` configs do not auto-migrate to the native path. To opt into reasoning round-trip, change the provider explicitly to `ollama-native` in thread settings.

---

## Recommended Models (16 GB VRAM)

### Tier 1  -  Proven tool calling

| Model | Quant | VRAM | Tool F1 | GGUF Source |
|-------|-------|------|---------|-------------|
| **Qwen 3 14B** | Q4_K_M | ~11 GB | 0.971 (Docker eval) | `bartowski/Qwen_Qwen3-14B-GGUF` |
| **Qwen 3 8B** | Q8_0 | ~8.5 GB | 0.933 (Docker eval) | `bartowski/Qwen_Qwen3-8B-GGUF` |

### Not recommended (as of April 2026)

| Model | Why |
|-------|-----|
| **Qwen 3.5 (any size)** | 21 documented chat template bugs in llama.cpp (QwenLM/Qwen3#1831), thinking-mode tool call escape bug (#20837), PEG parser crashes (#19905). Use unsloth GGUFs + `--reasoning off` if you must try it. |
| **Gemma 4 (any size)** | Array serialization bugs (#21384), infinite repetition loops (#21375), token leaks (#21316). PRs exist but aren't in prebuilt releases yet. |

### GGUF provider matters

**Use bartowski GGUFs** for tool calling. bartowski proactively fixes chat template bugs and uses imatrix quantization. Nearly every example in llama.cpp's `function-calling.md` uses bartowski GGUFs. Unsloth is the second choice (good for Qwen 3.5 specifically because they ship the template fixes). Qwen's official GGUFs sometimes have stale templates that cause llama.cpp to select the wrong parser.

---

## Technical: What We Fixed and Why

### Problem 1  -  Streaming tool-call name mangling

**Symptom**: tool call `nym_todo_list` arrives at the agent graph as `nym_todonym_todo_list`.

**Root cause**: llama.cpp's `peg-native` streaming parser generates a GBNF grammar for tool names. When two tools share a prefix (e.g. `nym_todo` and `nym_todo_list`), the grammar's ordered choice tries the shorter name first. In streaming mode, the parser commits to `nym_todo` on partial input, then backtracks to `nym_todo_list`  -  but emits **both** as separate streaming delta chunks with the same `tool_call` index. Standard OpenAI streaming clients (langchain-openai, openai-python) accumulate `name` deltas by concatenation → `"nym_todo" + "nym_todo_list" = "nym_todonym_todo_list"`.

**Fix** (`vendor/react_agent/providers.py`): sort tools by name length descending before `bind_tools()`, so the grammar tries `nym_todo_list` before `nym_todo`. This is a harmless client-side workaround  -  tool order doesn't affect model behavior, only the grammar ordering llama.cpp derives from the tool list.

### Problem 2  -  Streaming breaks local LLM tool calling generally

**Symptom**: various tool-call failures  -  mangled names, dropped calls, empty responses, infinite retry loops.

**Root cause**: the OpenAI streaming protocol for `tool_calls` deltas is a known-fragile area for local inference servers. OpenClaw (354k-star personal AI framework) has the identical bug (openclaw/openclaw#5769). llama.cpp, Ollama, and LM Studio all have documented streaming tool-call issues. The fundamental problem is that incremental PEG parsing with grammar constraints + streaming + thinking mode creates a combinatorial explosion of edge cases that each server handles differently.

**Fix** (`vendor/react_agent/providers.py`): detect when `base_url` points at a local host (`localhost`, `127.0.0.1`, `host.docker.internal`, etc.) and set `streaming=False` on the `ChatOpenAI` constructor. This makes the LLM return the full response in one shot, bypassing all incremental parsing bugs. CLIProxy sidecar URLs and ports `8317`/`8318` are excluded from this local-LLM heuristic because reasoning-token streaming depends on the normal streaming path.

**Trade-off**: local LLM responses appear all at once after a brief wait instead of streaming token by token. At 60+ tok/s this is barely noticeable (~2-3 seconds for a typical response).

### Problem 3  -  Empty chat bubbles with non-streaming LLM

**Symptom**: after fixing Problem 2, the frontend shows empty response bubbles. Tool calls render correctly, but the agent's text response is missing. Content IS stored in the database (visible on page reload) but not delivered live via SSE.

**Root cause** (`core/agent.py`): the async `astream()` SSE handler's `on_chat_model_end` branch only emitted content when `output.tool_calls` was also present (it was designed to catch preamble text alongside tool calls). When `streaming=False`, the entire response arrives via `on_chat_model_end` with no `tool_calls` (final answer case), so the content was silently dropped.

**Fix** (`core/agent.py`): broadened the `on_chat_model_end` handler to emit content regardless of whether `tool_calls` are also present. The existing guard `if not streamed_text_in_current_llm_call` prevents double-emission when streaming works normally (e.g. Claude via CLIProxy). Autonomous/ticker flows now consume this same `astream()` path through `core/stream_bridge.py`.

### Problem 4  -  Dropdown reset bug (frontend)

**Symptom**: selecting "Anthropic (Subscription)" in the Settings dropdown and saving silently reverts to "Anthropic (Direct API)" on reload.

**Root cause** (`SettingsPanel.svelte`): when switching FROM Direct API (which stores `base_url=""`) TO Subscription, the `llmBaseUrl` text field is empty. Saving sends `base_url=""` → the backend stores it as Direct API → reload maps it back to Direct.

**Fix** (`SettingsPanel.svelte`): added `$effect` auto-populate rules for `anthropic_proxy` (fills `http://cli-proxy-api:8317`) and `local_openai` (fills `http://host.docker.internal:8080/v1`) when the field is empty. This mirrors how the provider identity is computed from the base URL on load (`toDisplayProvider`)  -  now the inverse direction also works.

---

## Known Limitations

### Autonomous flows with small local models

Qwen 3 14B handles interactive tool calling reliably (list todos, create todos, handle errors gracefully). However, in autonomous flows (ticker-triggered TODO execution) with accumulated context (~9+ messages), the model sometimes:

- **Writes tool calls as plain text** instead of structured `tool_call` format (e.g. `nym_todo(todo_id="abc", status="done")` appearing as prose)
- **Skips follow-up actions**  -  executes the TODO task but doesn't mark it done

This is a model capability ceiling, not a parser bug. Claude handles multi-step autonomous chaining naturally; 14B-class local models don't reliably maintain tool-call discipline across long contexts.

**Mitigation**: use Claude (via CLIProxy or Direct API) for autonomous workflows. Use Local LLM for interactive chat. Per-thread provider overrides support this mixed approach  -  set the global default to Claude and override specific experimentation threads to Local LLM.

### Context window pressure

16 GB VRAM limits 14B Q4 models to ~16K context. Nymeria's system prompt + memories + TODOs consume ~4K tokens, leaving ~12K for conversation history. Long chats will hit auto-compaction much sooner than with Claude (which has 200K-1M context). Use `-ctk q8_0 -ctv q8_0` flags on llama-server to compress KV cache if you need more headroom.

### No streaming for local LLM

Responses appear all at once instead of token-by-token. This is an intentional trade-off for tool-calling reliability. The underlying issue is in llama.cpp's streaming tool-call parser, not in Nymeria. If/when llama.cpp fixes their streaming PEG parser bugs, the `streaming=False` override in `providers.py` can be removed to restore token-by-token delivery.

---

## AMD RDNA 4 (RX 9070 XT) Specifics

- **Vulkan is the right backend**. ROCm 6.4.1+ officially supports gfx1201 but has friction on consumer cards (wavefront size 32 vs CDNA's 64, AITER kernel incompatibilities). Vulkan works universally with zero configuration.
- **Ollama on Windows does NOT work on RDNA 4**  -  silently falls back to CPU (ollama#11542). Don't use it.
- **LM Studio's bundled ROCm is too old** for gfx1201. Use its Vulkan mode.
- **RADV over AMDVLK**: RADV is up to 4x faster for dense model prefill on RDNA4 (Linux). On Windows, the AMD proprietary Vulkan driver is the only option and works well.
- **bf16 support**: RDNA 4 has native bf16 via KHR_coopmat. Recent llama.cpp builds leverage this.

---

## Troubleshooting

### llama-server won't start or GPU isn't detected
- Check `vulkaninfo --summary`  -  the GPU should appear as a discrete device
- Ensure you downloaded the Vulkan build of llama.cpp (not the CPU-only or CUDA build)
- Try `-ngl 0` to confirm the binary works on CPU, then increase to `-ngl 99`

### Tool calls are mangled or tool names are wrong
- Ensure you're using a **bartowski GGUF** (not Qwen's official  -  different embedded templates)
- Check that `streaming=False` is active: look for `[LLM] Local base_url detected` in `docker logs nymeria-api`
- Verify the tool-ordering sort is in place: `providers.py` should have `sorted(tools, key=lambda t: len(t.name), reverse=True)` before `bind_tools()`

### Responses appear in the database but not the frontend
- The `on_chat_model_end` SSE handler fix must be applied  -  check `core/agent.py` around the `on_chat_model_end` handler
- Refresh the frontend (Ctrl+Shift+R) to clear cached JS

### Container can't reach llama-server on the host
- Verify `extra_hosts: ["host.docker.internal:host-gateway"]` is in `docker-compose.yml` for the api service
- Test from inside the container: `docker exec nymeria-api curl -s http://host.docker.internal:8080/v1/models`
- If Windows Defender Firewall is blocking, add an inbound rule for `llama-server.exe`

### Model identifies as Claude Code instead of Nymeria
- This is a CLIProxy version issue, not a local LLM issue. Pin CLIProxy to v6.9.0. See the CLIProxy section in CLAUDE.md.
