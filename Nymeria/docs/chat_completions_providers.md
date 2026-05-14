# OpenAI-Compatible Chat Providers

Nymeria supports Anthropic through its native Messages API and a broad set of
providers through the OpenAI Chat Completions wire shape. The provider registry
lives in `nymeria/config/llm_providers.py`; the live API catalog is exposed at
`GET /settings/llm/providers`.

This list was seeded from Nymeria's existing provider code, `/opt/openclaw`,
`/opt/hermes-agent`, `/opt/opencode`, and `models.dev`. Provider model IDs are
not maintained as static lists: desktop/mobile call `GET /models/available`,
which asks the selected provider's `/models` endpoint using the effective base
URL and credential.

## Runtime Behavior

- Auth lookup order is per-thread explicit key/reference, encrypted credential
  vault, provider-specific settings/env vars, then `LLM_API_KEY`.
- Credential-vault records for LLMs should use `provider=<provider-id>`,
  `kind=api_key` or `llm_provider`, secret field `api_key`, and allowed target
  `llm_provider:<provider-id>` or `llm_provider:*`.
- Base URL lookup order is per-thread override, credential-vault `base_url`,
  provider-specific base URL env var, then the registry default.
- Chat Completions token usage is consumed from standard
  `prompt_tokens` / `completion_tokens` metadata when the provider or SDK
  returns it. LangChain `usage_metadata.input_tokens/output_tokens` and
  Anthropic `usage.input_tokens/output_tokens` are also supported.
- Context-window size is not standardized by Chat Completions. Nymeria caches
  `context_length`, `context_window`, `max_context_tokens`, and similar fields
  from provider `/models` responses when present; otherwise it falls back to
  OpenRouter metadata or static defaults.
- Standard Chat Completions params wired through the provider adapter:
  `temperature`, `max_tokens`, `top_p`, `frequency_penalty`, and
  `presence_penalty`. `top_k` is not an OpenAI Chat Completions standard; it is
  passed as a provider-specific extra only for compatible backends that accept
  it.
- Responses API mode is first-class for providers that advertise it in the
  registry, currently OpenAI, OpenRouter, Azure OpenAI, and xAI. Responses uses
  `max_output_tokens`; Chat Completions uses `max_tokens`.

## First-Class Providers

| Provider ID | Provider | Default base URL | API key env vars | Notes |
|-------------|----------|------------------|------------------|-------|
| `openai` | OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` | Supports Chat Completions and Responses. |
| `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | Supports Chat Completions and Responses. |
| `azure-openai` | Azure OpenAI | custom | `AZURE_OPENAI_API_KEY`, `AZURE_API_KEY` | Set base URL to the `/openai/v1/` deployment endpoint. |
| `xai` | xAI | `https://api.x.ai/v1` | `XAI_API_KEY` | Supports Chat Completions and Responses. |
| `google` | Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY` | Gemini OpenAI-compatible adapter. |
| `groq` | Groq | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | Chat Completions compatible. |
| `deepseek` | DeepSeek | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | Also accepts `/v1` compatibility aliases. |
| `mistral` | Mistral AI | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` | Chat Completions compatible. |
| `togetherai` | Together AI | `https://api.together.ai/v1` | `TOGETHER_API_KEY` | Alias: `together`. |
| `fireworks-ai` | Fireworks AI | `https://api.fireworks.ai/inference/v1` | `FIREWORKS_API_KEY` | Alias: `fireworks`. |
| `perplexity` | Perplexity | `https://api.perplexity.ai` | `PERPLEXITY_API_KEY` | Chat Completions compatible. |
| `cerebras` | Cerebras | `https://api.cerebras.ai/v1` | `CEREBRAS_API_KEY` | Chat Completions compatible. |
| `sambanova` | SambaNova | `https://api.sambanova.ai/v1` | `SAMBANOVA_API_KEY` | Chat Completions compatible. |
| `nvidia` | NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` | Alias: `nvidia-nim`. |
| `huggingface` | Hugging Face Inference Providers | `https://router.huggingface.co/v1` | `HF_TOKEN`, `HUGGINGFACE_API_KEY` | OpenAI-compatible router. |
| `deepinfra` | DeepInfra | `https://api.deepinfra.com/v1/openai` | `DEEPINFRA_API_KEY` | Chat Completions compatible. |
| `moonshotai` | Moonshot AI / Kimi | `https://api.moonshot.ai/v1` | `MOONSHOT_API_KEY` | Aliases: `moonshot`, `kimi`. |
| `alibaba` | Alibaba Cloud Model Studio | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY`, `ALIBABA_API_KEY` | Aliases: `dashscope`, `qwen`. |
| `zai` | Z.ai | `https://api.z.ai/api/paas/v4` | `ZAI_API_KEY`, `Z_AI_API_KEY`, `ZHIPU_API_KEY`, `GLM_API_KEY` | Alias: `glm`. |
| `zhipuai` | Zhipu AI BigModel | `https://open.bigmodel.cn/api/paas/v4` | `ZHIPU_API_KEY`, `GLM_API_KEY` | Chat Completions compatible. |
| `qianfan` | Baidu Qianfan | `https://qianfan.baidubce.com/v2` | `QIANFAN_API_KEY` | Chat Completions compatible. |
| `volcengine` | Volcengine Ark | `https://ark.cn-beijing.volces.com/api/v3` | `VOLCANO_ENGINE_API_KEY`, `ARK_API_KEY`, `VOLCENGINE_API_KEY` | Alias: `doubao`. |
| `tencent-tokenhub` | Tencent TokenHub | `https://tokenhub.tencentmaas.com/v1` | `TENCENT_TOKENHUB_API_KEY` | Alias: `tencent`. |
| `novita-ai` | Novita AI | `https://api.novita.ai/openai` | `NOVITA_API_KEY` | Chat Completions compatible. |
| `siliconflow` | SiliconFlow | `https://api.siliconflow.com/v1` | `SILICONFLOW_API_KEY` | Chat Completions compatible. |
| `arcee` | Arcee AI | `https://api.arcee.ai/api/v1` | `ARCEEAI_API_KEY`, `ARCEE_API_KEY` | Chat Completions compatible. |
| `chutes` | Chutes | `https://llm.chutes.ai/v1` | `CHUTES_API_KEY`, `CHUTES_OAUTH_TOKEN` | Chat Completions compatible. |
| `venice` | Venice AI | `https://api.venice.ai/api/v1` | `VENICE_API_KEY` | Chat Completions compatible. |
| `requesty` | Requesty | `https://router.requesty.ai/v1` | `REQUESTY_API_KEY` | Gateway provider. |
| `poe` | Poe | `https://api.poe.com/v1` | `POE_API_KEY` | Chat Completions compatible. |
| `github-models` | GitHub Models | `https://models.github.ai/inference` | `GITHUB_TOKEN` | Alias: `github`. |
| `cloudflare-workers-ai` | Cloudflare Workers AI | `https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1` | `CLOUDFLARE_API_KEY`, `CLOUDFLARE_API_TOKEN` | Requires account ID expansion. |
| `cloudflare-ai-gateway` | Cloudflare AI Gateway | custom | `CLOUDFLARE_API_TOKEN` | Set `CLOUDFLARE_AI_GATEWAY_BASE_URL`. |
| `vercel` | Vercel AI Gateway | `https://ai-gateway.vercel.sh/v1` | `AI_GATEWAY_API_KEY`, `VERCEL_AI_GATEWAY_API_KEY` | Alias: `ai-gateway`. |
| `opencode` | OpenCode Zen | `https://opencode.ai/zen/v1` | `OPENCODE_API_KEY` | Reference provider from opencode. |
| `kilocode` | Kilo Code Gateway | `https://api.kilo.ai/api/gateway` | `KILOCODE_API_KEY`, `KILO_API_KEY` | Alias: `kilo`. |

## Local / Self-Hosted Providers

These providers are OpenAI-compatible but usually do not require an API key.
Nymeria disables streaming for local inference hosts where tool-call deltas are
known to be unreliable.

| Provider ID | Default base URL | API key env vars |
|-------------|------------------|------------------|
| `ollama` | `http://localhost:11434/v1` | `OLLAMA_API_KEY` |
| `ollama-cloud` | `https://ollama.com/v1` | `OLLAMA_API_KEY` |
| `lmstudio` | `http://127.0.0.1:1234/v1` | `LMSTUDIO_API_KEY`, `LM_API_KEY` |
| `llamacpp` | `http://localhost:8080/v1` | `LLAMACPP_API_KEY` |
| `vllm` | `http://localhost:8000/v1` | `VLLM_API_KEY` |
| `localai` | `http://localhost:8080/v1` | `LOCALAI_API_KEY` |
| `litellm` | `http://localhost:4000` | `LITELLM_API_KEY` |
| `tgi` | `http://localhost:8080/v1` | `TGI_API_KEY` |

## Additional Registered Providers

The registry also includes these OpenAI-compatible providers from `models.dev`
and reference implementations. They use the same runtime path and are available
through API/provider IDs even when they are not surfaced prominently in the UI:

`302ai`, `abacus`, `abliteration-ai`, `alibaba-cn`, `ambient`, `auriko`,
`bailing`, `baseten`, `berget`, `clarifai`, `claudinio`,
`cloudferro-sherlock`, `cortecs`, `databricks`, `digitalocean`, `dinference`,
`drun`, `evroc`, `fastrouter`, `firepass`, `friendli`, `frogbot`,
`helicone`, `hpc-ai`, `iflowcn`, `inception`, `inference`, `io-net`,
`jiekou`, `kuae-cloud-coding-plan`, `llama`, `llmgateway`, `lucidquery`,
`meganova`, `mixlayer`, `moark`, `modelscope`, `moonshotai-cn`, `morph`,
`nano-gpt`, `nebius`, `neuralwatt`, `nova`, `ovhcloud`, `perplexity-agent`,
`privatemode-ai`, `qihang-ai`, `qiniu-ai`, `regolo-ai`, `sarvam`,
`scaleway`, `siliconflow-cn`, `stackit`, `submodel`, `synthetic`,
`tencent-coding-plan`, `the-grid-ai`, `upstage`, `vivgrid`, `vultr`,
`wafer.ai`, `wandb`, `xiaomi`, `xiaomi-token-plan-ams`,
`xiaomi-token-plan-cn`, `xiaomi-token-plan-sgp`, `xpersona`,
`zai-coding-plan`, `zenmux`, and `zhipuai-coding-plan`.

Unknown providers can still be tested as custom OpenAI-compatible endpoints by
supplying an explicit base URL. The backend will not invent a static model list
for them; the frontend will query `/models` and still allow manual model IDs if
that endpoint is unavailable.
