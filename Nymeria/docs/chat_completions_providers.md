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

Reference coverage checked on 2026-05-14:

- Current `models.dev/api.json` has 124 provider entries. Nymeria registers all
  entries that expose an OpenAI-compatible endpoint or SDK path suitable for the
  generic Chat Completions adapter, plus local/self-hosted runtimes.
- `/opt/opencode` consumes `models.dev` and `@ai-sdk/openai-compatible`; Nymeria
  mirrors the compatible provider IDs where the backend can call them with a
  base URL and bearer token.
- `/opt/hermes-agent` adds Hermes-only profiles such as `nous`, `gmi`,
  `azure-foundry`, `qwen-oauth`, and coding-plan endpoints; these are registered
  with caveats in the provider notes.
- `/opt/openclaw` adds coding-plan variants for Alibaba/Qwen, StepFun,
  Volcengine/Doubao, and BytePlus ModelArk. These are registered as separate
  provider IDs because they use different base URLs and sometimes different key
  tiers.
- Providers that are not Chat Completions compatible, or require a different
  native transport that Nymeria does not implement here, are deliberately not
  registered as OpenAI-chat providers. Examples: native Anthropic/Bedrock,
  MiniMax and Kimi coding Anthropic-compatible endpoints, and Copilot ACP.

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
- Production-readiness checks live behind `POST /settings/llm/test-suite`.
  The suite resolves auth from a submitted key, credential vault, settings, or
  env; probes `/models`; prefers free models when metadata exposes pricing;
  runs a small non-streaming chat completion; and can force a tool call to check
  Nymeria agent compatibility. It returns sanitized step results and never
  writes settings.

## First-Class Providers

| Provider ID | Provider | Default base URL | API key env vars | Notes |
|-------------|----------|------------------|------------------|-------|
| `openai` | OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` | Supports Chat Completions and Responses. |
| `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | Supports Chat Completions and Responses. |
| `azure-openai` | Azure OpenAI | custom | `AZURE_OPENAI_API_KEY`, `AZURE_API_KEY` | Set base URL to the `/openai/v1/` deployment endpoint. |
| `azure-foundry` | Azure AI Foundry | custom | `AZURE_FOUNDRY_API_KEY`, `AZURE_OPENAI_AUTH_TOKEN`, `AZURE_API_KEY` | Set base URL to the Foundry `/openai/v1/` endpoint. |
| `xai` | xAI | `https://api.x.ai/v1` | `XAI_API_KEY` | Supports Chat Completions and Responses. |
| `google` | Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY`, `GOOGLE_GENERATIVE_AI_API_KEY` | Gemini OpenAI-compatible adapter. |
| `google-vertex` | Google Vertex AI | custom | `GOOGLE_VERTEX_ACCESS_TOKEN` | Requires a Google Cloud OAuth access token; token refresh is not automatic. |
| `groq` | Groq | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | Chat Completions compatible. |
| `deepseek` | DeepSeek | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | Also accepts `/v1` compatibility aliases. |
| `mistral` | Mistral AI | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` | Chat Completions compatible. |
| `cohere` | Cohere | `https://api.cohere.ai/compatibility/v1` | `COHERE_API_KEY` | Cohere compatibility API for OpenAI SDK clients. |
| `togetherai` | Together AI | `https://api.together.ai/v1` | `TOGETHER_API_KEY` | Alias: `together`. |
| `fireworks-ai` | Fireworks AI | `https://api.fireworks.ai/inference/v1` | `FIREWORKS_API_KEY` | Alias: `fireworks`. |
| `perplexity` | Perplexity | `https://api.perplexity.ai` | `PERPLEXITY_API_KEY` | Chat Completions compatible. |
| `cerebras` | Cerebras | `https://api.cerebras.ai/v1` | `CEREBRAS_API_KEY` | Chat Completions compatible. |
| `sambanova` | SambaNova | `https://api.sambanova.ai/v1` | `SAMBANOVA_API_KEY` | Chat Completions compatible. |
| `nvidia` | NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | `NVIDIA_API_KEY` | Alias: `nvidia-nim`. |
| `huggingface` | Hugging Face Inference Providers | `https://router.huggingface.co/v1` | `HF_TOKEN`, `HUGGINGFACE_API_KEY` | OpenAI-compatible router. |
| `deepinfra` | DeepInfra | `https://api.deepinfra.com/v1/openai` | `DEEPINFRA_API_KEY` | Chat Completions compatible. |
| `moonshotai` | Moonshot AI / Kimi | `https://api.moonshot.ai/v1` | `MOONSHOT_API_KEY` | Aliases: `moonshot`, `kimi`. |
| `aihubmix` | AIHubMix | `https://aihubmix.com/v1` | `AIHUBMIX_API_KEY` | Routing gateway; backup host is `https://api.aihubmix.com`. |
| `alibaba` | Alibaba Cloud Model Studio | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY`, `ALIBABA_API_KEY` | Aliases: `dashscope`, `qwen`. |
| `alibaba-coding-plan` | Alibaba Cloud Coding Plan | `https://coding-intl.dashscope.aliyuncs.com/v1` | `ALIBABA_CODING_PLAN_API_KEY`, `DASHSCOPE_API_KEY` | Dedicated coding-plan key tier. |
| `alibaba-coding-plan-cn` | Alibaba Cloud Coding Plan China | `https://coding.dashscope.aliyuncs.com/v1` | `ALIBABA_CODING_PLAN_API_KEY`, `DASHSCOPE_API_KEY` | China-region coding-plan endpoint. |
| `qwen-oauth` | Qwen Portal | `https://portal.qwen.ai/v1` | `QWEN_API_KEY` | Hermes reference profile; prefer Coding Plan for normal API-key setup. |
| `zai` | Z.ai | `https://api.z.ai/api/paas/v4` | `ZAI_API_KEY`, `Z_AI_API_KEY`, `ZHIPU_API_KEY`, `GLM_API_KEY` | Alias: `glm`. |
| `zhipuai` | Zhipu AI BigModel | `https://open.bigmodel.cn/api/paas/v4` | `ZHIPU_API_KEY`, `GLM_API_KEY` | Chat Completions compatible. |
| `qianfan` | Baidu Qianfan | `https://qianfan.baidubce.com/v2` | `QIANFAN_API_KEY` | Chat Completions compatible. |
| `stepfun` | StepFun | `https://api.stepfun.ai/v1` | `STEPFUN_API_KEY` | Standard StepFun OpenAI-compatible endpoint. |
| `stepfun-plan` | StepFun Step Plan | `https://api.stepfun.ai/step_plan/v1` | `STEPFUN_API_KEY` | OpenClaw/Hermes coding-plan endpoint. |
| `volcengine` | Volcengine Ark | `https://ark.cn-beijing.volces.com/api/v3` | `VOLCANO_ENGINE_API_KEY`, `ARK_API_KEY`, `VOLCENGINE_API_KEY` | Alias: `doubao`. |
| `volcengine-coding-plan` | Volcengine Ark Coding Plan | `https://ark.cn-beijing.volces.com/api/coding/v3` | `VOLCANO_ENGINE_API_KEY`, `ARK_API_KEY`, `VOLCENGINE_API_KEY` | Coding-plan endpoint for Doubao/Ark coding models. |
| `byteplus` | BytePlus ModelArk | `https://ark.ap-southeast.bytepluses.com/api/v3` | `BYTEPLUS_API_KEY`, `ARK_API_KEY` | International ModelArk OpenAI-compatible endpoint. |
| `byteplus-coding-plan` | BytePlus ModelArk Coding Plan | `https://ark.ap-southeast.bytepluses.com/api/coding/v3` | `BYTEPLUS_API_KEY`, `ARK_API_KEY` | Coding-plan endpoint for OpenClaw-compatible tools. |
| `tencent-tokenhub` | Tencent TokenHub | `https://tokenhub.tencentmaas.com/v1` | `TENCENT_TOKENHUB_API_KEY` | Alias: `tencent`. |
| `novita-ai` | Novita AI | `https://api.novita.ai/openai` | `NOVITA_API_KEY` | Chat Completions compatible. |
| `siliconflow` | SiliconFlow | `https://api.siliconflow.com/v1` | `SILICONFLOW_API_KEY` | Chat Completions compatible. |
| `arcee` | Arcee AI | `https://api.arcee.ai/api/v1` | `ARCEEAI_API_KEY`, `ARCEE_API_KEY` | Chat Completions compatible. |
| `chutes` | Chutes | `https://llm.chutes.ai/v1` | `CHUTES_API_KEY`, `CHUTES_OAUTH_TOKEN` | Chat Completions compatible. |
| `venice` | Venice AI | `https://api.venice.ai/api/v1` | `VENICE_API_KEY` | Chat Completions compatible. |
| `requesty` | Requesty | `https://router.requesty.ai/v1` | `REQUESTY_API_KEY` | Gateway provider. |
| `poe` | Poe | `https://api.poe.com/v1` | `POE_API_KEY` | Chat Completions compatible. |
| `github-models` | GitHub Models | `https://models.github.ai/inference` | `GITHUB_TOKEN` | Alias: `github`. |
| `github-copilot` | GitHub Copilot | `https://api.githubcopilot.com` | `COPILOT_GITHUB_TOKEN`, `GITHUB_COPILOT_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` | Requires a Copilot bearer token; a normal GitHub PAT may not be sufficient. |
| `cloudflare-workers-ai` | Cloudflare Workers AI | `https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/ai/v1` | `CLOUDFLARE_API_KEY`, `CLOUDFLARE_API_TOKEN` | Requires account ID expansion. |
| `cloudflare-ai-gateway` | Cloudflare AI Gateway | custom | `CLOUDFLARE_API_TOKEN` | Set `CLOUDFLARE_AI_GATEWAY_BASE_URL`. |
| `vercel` | Vercel AI Gateway | `https://ai-gateway.vercel.sh/v1` | `AI_GATEWAY_API_KEY`, `VERCEL_AI_GATEWAY_API_KEY` | Alias: `ai-gateway`. |
| `opencode` | OpenCode Zen | `https://opencode.ai/zen/v1` | `OPENCODE_API_KEY` | Reference provider from opencode. |
| `kilocode` | Kilo Code Gateway | `https://api.kilo.ai/api/gateway` | `KILOCODE_API_KEY`, `KILO_API_KEY` | Alias: `kilo`. |
| `gmi` | GMI Cloud | `https://api.gmi-serving.com/v1` | `GMI_API_KEY` | Some accounts require an `X-Organization-ID` header, which Nymeria does not expose yet. |
| `nous` | Nous Research | `https://inference.nousresearch.com/v1` | `NOUS_API_KEY` | Hermes reference provider; some accounts use OAuth/device-code credentials. |
| `v0` | Vercel v0 | `https://api.v0.dev/v1` | `V0_API_KEY` | OpenAI-compatible coding/design model API. |

## Provider Documentation Links

Each provider spec has a `docs_url` that is returned by
`GET /settings/llm/providers`. Useful references for the trickier providers:

| Provider | Documentation |
|----------|---------------|
| Alibaba Coding Plan | https://www.alibabacloud.com/help/en/model-studio/other-tools-coding-plan |
| Azure AI Foundry | https://learn.microsoft.com/azure/foundry/foundry-models/concepts/endpoints |
| BytePlus ModelArk | https://docs.byteplus.com/en/docs/modelark/1330626 |
| Cohere compatibility API | https://docs.cohere.com/v2/docs/compatibility-api |
| GMI Cloud | https://docs.gmicloud.ai/inference-engine/api-reference/llm-api-reference |
| Google Vertex OpenAI compatibility | https://cloud.google.com/vertex-ai/generative-ai/docs/start/openai |
| StepFun OpenAI migration | https://platform.stepfun.ai/docs/en/guides/developer/openai |
| Vercel v0 Model API | https://vercel.com/docs/v0/api |

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
