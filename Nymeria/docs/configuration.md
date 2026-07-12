# Nymeria Configuration

All configuration is done via environment variables. Source checkouts load
`.env`, `config.env`, and `.env.docker` from the backend root if present;
when the same variable appears in multiple files, `.env.docker` has the
highest dotenv-file precedence. Packaged `nymeria` installs use
`~/.nymeria/config.env` by default. Copy `.env.docker.example` to
`.env.docker` for the full local/Docker template, run `nymeria init` for
packaged setup, or create `.env` manually for a lighter source-checkout setup.

**Note:** Nymeria validates configuration on startup. If required keys are missing, you'll see clear error messages with instructions.

## Runtime Settings Updates

Admins can update mapped server settings at runtime with `PATCH /settings`.
The endpoint writes the selected dotenv file, syncs mapped values into
`os.environ`, clears the cached `Settings` object, assigns the refreshed
settings to the live agent, and returns `restart_required`.

The following settings are applied to future graph builds immediately and also
clear/rebuild the current default graph caches:

- LLM provider/model/fallback fields, sampling fields, reasoning fields,
  `LLM_BASE_URL`, `OPENAI_API_MODE`, stream retry fields, and fallback hold
  duration
- LLM provider credentials: Anthropic, Anthropic direct, OpenAI, and OpenRouter
- `TOOL_OUTPUT_MAX_CHARS`

Other hot-updated settings are visible to code paths that read
`agent.settings` after the patch. Existing compiled graphs may keep their old
configuration until a graph rebuild or API restart unless the setting is in the
graph-sensitive list above.

These keys persist through `PATCH /settings` but require an API restart to take
full effect:

- `REDIS_URL`, `REDIS_ENABLED`
- `POSTGRES_URI`, `NYMERIA_DATA_DIR`
- `DISCORD_BOT_TOKEN`, `DISCORD_WEBHOOK_URL`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_DEFAULT_CHAT_ID`

When any of those keys are patched, the API response includes
`"restart_required": true`.

## Environment Variables

### LLM Configuration

These variables are deployment-wide server defaults, not per-user account preferences. In a multi-user deployment, every thread that does not set a per-thread LLM override inherits the same `LLM_PROVIDER`, `LLM_MODEL`, API key, and `LLM_BASE_URL`; if an admin changes them through Settings → LLM, the change affects all users on that server. User-specific routing is currently done with per-thread overrides.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LLM_PROVIDER` | Yes | `anthropic` | LLM provider ID. Native partner-package paths: `anthropic`, `openai`, `google` (Gemini via langchain-google-genai), `bedrock` (AWS via langchain-aws ChatBedrockConverse), `ollama` (native protocol via langchain-ollama). OpenAI-compatible IDs include `openrouter`, `xai`, `groq`, `deepseek`, `mistral`, `lmstudio`, plus the full registry of supported OpenAI-compatible providers. |
| `LLM_PROVIDER_ROUTE` | No | provider default | Adapter route for providers with more than one supported path. Valid values: `native`, `openai_compat`, `anthropic_messages`. Exposed for `google` and `ollama` (`native`/`openai_compat`, default `native`) and for the Claude-serving gateways `litellm`, `opencode`, `zenmux`, `requesty`, `fastrouter`, `poe` (`openai_compat`/`anthropic_messages`, default `openai_compat`). Per-thread settings can override it. |
| `LLM_MODEL` | Yes | `claude-sonnet-4-6` | Model identifier for the provider (the primary/default tier) |
| `LLM_FAST_MODEL` | No | provider-aware | Fast model tier used by the `/fast` command and the `fast` alias (e.g. in `spawn_thread`). A model id, or `provider:model-id` to route the tier to a different provider with its own credentials. When unset, picks a provider-aware default. |
| `LLM_SMART_MODEL` | No | primary model | Smart/high-capability model tier used by the `/smart` command and the `smart` alias. A model id, or `provider:model-id` for a different provider. When unset, resolves to `LLM_MODEL`. |
| `LLM_BACKGROUND_MODEL` | No | primary model | Background/utility model tier used by the `/background` command and the `background` alias. Powers the `extraction_prompt` step (`fetch_url_nymeria` and `file_read`) now, and more background tasks later. A model id, or `provider:model-id` for a different provider. A small local model works well (no tool calling needed). When unset, resolves to `LLM_MODEL`. |
| `LLM_BACKGROUND_BASE_URL` | No | (inherit provider) | Optional base URL override for the background tier (e.g. a local model server or CLIProxy). Blank inherits the resolved provider's base URL like the fast/smart tiers. |
| `LLM_FALLBACK_MODELS` | No | `anthropic:claude-haiku-4-5-20251001` | Comma-separated ordered fallback models tried by the backend after primary retries are exhausted for a transient provider/transport error before output starts. Entries use the active provider by default, or `provider:model-id` for any known provider in the LLM registry (so one provider's outage does not also disable the fallback). CLI shortcut: `/fallback`. |
| `LLM_TEMPERATURE` | No | `1.0` | Sampling temperature (0.0 - 2.0) |

#### Native partner-package providers

Three provider IDs route through dedicated `langchain-<provider>` packages instead of the OpenAI-compatible adapter by default. Use the native route when reasoning content must round-trip across tool follow-ups.

| Provider ID | Required env vars | Notes |
|-------------|-------------------|-------|
| `google` | `GEMINI_API_KEY` (or `GOOGLE_GENERATIVE_AI_API_KEY`) | Routes through `langchain-google-genai`. Gemini 3+ thought signatures round-trip natively; the OpenAI-compat shim drops them. Gemini 4.x SDK has a documented 50-90% latency increase on small Flash calls from the gRPC-to-REST transport switch. `max_retries=0` is interpreted as the SDK default of 5; Nymeria sets `max_retries=1` to actually disable internal retries. |
| `bedrock` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (plus optional `AWS_SESSION_TOKEN`); `AWS_REGION` (or `AWS_DEFAULT_REGION`) | Routes through `langchain-aws` `ChatBedrockConverse`. Credentials resolved through the boto3 default chain. Optional `AWS_BEDROCK_ENDPOINT_URL` for VPC endpoints. |
| `ollama` | none (no API key) | Defaults to `langchain-ollama` against Ollama's native `/api/chat` protocol. Use this for reasoning round-trip on `qwen3` / `deepseek-r1` / `gpt-oss`. Set `LLM_PROVIDER_ROUTE=openai_compat` or a per-thread route override to use Ollama's `/v1/chat/completions` shim. See [`local-llm.md`](local-llm.md) for the full Ollama native vs OpenAI-compat split. |

Existing thread configs storing `provider="google"` or `provider="ollama"` use the native route unless a global or per-thread `provider_route="openai_compat"` override is set. The old `ollama-native` provider id is accepted as an alias for `ollama` with the native route.

#### Claude on gateways: the `anthropic_messages` route

A gateway's OpenAI-compatible path corrupts Claude's signed extended-thinking blocks (any Anthropic/OpenAI format translation drops the signature), so multi-turn reasoning passback silently breaks for Claude models behind a gateway. Gateways that serve Claude and lose the signature on their compat path (`litellm`, `opencode`, `zenmux`, `requesty`, `fastrouter`, `poe`) advertise an `anthropic_messages` route in addition to `openai_compat` (default stays `openai_compat`). Setting `provider_route="anthropic_messages"` for one of these (global env or per-thread) routes the gateway's Claude traffic through `langchain-anthropic` against the gateway's own `/v1/messages` endpoint, restoring native thinking and signature round-trip. The gateway key and base URL are resolved through the normal provider env vars. Signature-safe gateways (`openrouter`, `vercel`, `aihubmix`) round-trip Claude reasoning via `reasoning_details` on the compat path and do not advertise the route.

### Advanced LLM Settings (Optional)

These settings give power users fine-grained control over LLM behavior. All are optional and only sent to the API if explicitly set.

| Variable | Default | Range | Description |
|----------|---------|-------|-------------|
| `LLM_MAX_TOKENS` | (model limit) | 1 - 1,000,000 | Maximum output tokens |
| `LLM_TOP_P` | (provider default) | 0.0 - 1.0 | Nucleus sampling threshold |
| `LLM_TOP_K` | (provider default) | 1 - 100 | Top-k sampling (limits vocabulary per step) |
| `LLM_FREQUENCY_PENALTY` | (provider default) | -2.0 - 2.0 | Reduce repetition of token sequences |
| `LLM_PRESENCE_PENALTY` | (provider default) | -2.0 - 2.0 | Encourage new topics |
| `LLM_REASONING_EFFORT` | (none) | off/low/medium/high/xhigh/max | Reasoning effort for compatible models. `off` explicitly disables thinking; unset inherits provider behavior. Levels a model does not support are adjusted onto its supported range before the request is sent (over-asks drop to the model's ceiling; an unsupported `off` rises to its lowest level); invalid values fail settings validation |
| `LLM_EXTENDED_THINKING` | `false` | true/false | Enable extended thinking/reasoning for compatible models. Command shortcut on every surface: `/think on\|off\|low\|medium\|high\|xhigh\|max [global\|thread]` (aliases `/reasoning`, `/thinking`) sets both fields in one command, thread-scoped when a thread is active. `/fast` toggles the active thread between `LLM_MODEL` and `LLM_FAST_MODEL`; `/fast <prompt>` uses the fast model for that turn only. `/smart` does the same with `LLM_SMART_MODEL`. Both are also in the central command registry (available on desktop and bots), and `fast`/`smart`/`default` work as `llm_model` aliases in `spawn_thread`. |
| `LLM_USE_MODEL_DEFAULTS` | `false` | true/false | Use model-specific defaults for temperature, top_p, and frequency penalty instead of global values. When enabled, these params are not sent to the API  -  the provider applies the model's own optimal defaults. |
| `LLM_BASE_URL` | (provider default) | URL | Override API endpoint for native Anthropic or OpenAI-compatible providers. For `anthropic` CLIProxy, use the root URL with no `/v1` suffix because `ChatAnthropic` appends `/v1/messages`; for OpenAI-compatible endpoints, use the provider's documented base URL, usually ending in `/v1`. Leave unset to use the registry default or a credential-vault base URL. |
| `LLM_CONTEXT_LENGTH` | auto | 1,000 - 2,000,000 | Manual context-window override for local endpoints or proxies that do not report context metadata. Also available per thread as `context_length`. |
| `LLM_OLLAMA_NUM_CTX` | auto | 1,000 - 2,000,000 | Ollama runtime context override sent as `extra_body.options.num_ctx` on Chat Completions requests. Also available per thread as `ollama_num_ctx`. |
| `OPENAI_API_MODE` | `responses` | responses/chat_completions | API mode for OpenAI-compatible providers. `responses` is used only for providers that advertise Responses support in the registry; unsupported providers fall back to Chat Completions. |
| `LLM_STREAM_MAX_RETRIES` | `2` | 0 - 10 | Retries for transient LLM call/stream failures. If a streaming call fails after partial output, Nymeria rewinds to the latest checkpoint and retries from that stable point. |
| `LLM_STREAM_RETRY_INITIAL_DELAY` | `1.0` | 0 - 60 | Initial retry backoff delay in seconds |
| `LLM_STREAM_RETRY_MAX_DELAY` | `8.0` | 0 - 300 | Maximum retry backoff delay in seconds |
| `LLM_FALLBACK_HOLD_SECONDS` | `7200` | 0 - 604800 | Seconds to keep a fallback provider/model active for a thread after primary retries are exhausted. `0` disables the timed hold. |

**Note:** For model dropdowns and context metadata, Nymeria asks the selected provider's `/models` endpoint through `GET /models/available`. Provider-returned context fields such as `context_length`, `context_window`, or `max_context_tokens` are cached for frontend context-window percentage calculations. Chat Completions itself standardizes usage token fields, not context-window limits. Local endpoints get extra probing: Ollama `/api/show`, LM Studio `/api/v1/models`, llama.cpp `/props`, and common OpenAI-compatible `max_model_len` fields are checked when the base URL is loopback, container-local, private LAN, or Tailscale.

**Also note:** `LLM_EXTENDED_THINKING`, `LLM_USE_MODEL_DEFAULTS`, `OPENAI_API_MODE`, and provider-aware `LLM_BASE_URL` overrides are implemented in settings and runtime behavior, so they are safe to rely on even though some older docs may mention proxy behavior separately. OpenRouter Responses reasoning is displayed only when the provider emits plaintext reasoning fields; malformed inline `<think>` text that arrives as normal answer text is stripped from display and replay.

`LLM_FALLBACK_MODELS` is backend-owned, so it applies to every chat surface:
desktop, mobile, CLI, bots, triggers, scheduled TODOs, and callable-thread
invocations. Nymeria retries the active provider/model first for retryable
failures such as 429s, 5xx responses, timeouts, or transport errors. If a
streaming failure happens after partial output, Nymeria discards the uncommitted
model output, re-enters the graph from the latest checkpoint, and retries from
that stable boundary. Live frontends receive `provider_retry` events with the
backoff delay and a `rewound` flag when replay was needed. If retries are
exhausted, Nymeria emits
`provider_fallback`, switches to the next configured fallback, and keeps that
fallback active for the thread for `LLM_FALLBACK_HOLD_SECONDS` seconds
(default: 2 hours). Expiry is lazy: if the hold expires during an active turn,
the fallback is cleared after the turn releases the thread lock. After
streaming starts, Nymeria does not switch models because that would duplicate
visible output.

### API Keys

Set the API key for your chosen provider:

| Variable | Provider | Required |
|----------|----------|----------|
| `ANTHROPIC_API_KEY` | Anthropic | If using `anthropic` provider |
| `ANTHROPIC_DIRECT_API_KEY` | Anthropic | Optional direct Anthropic `sk-ant-*` key. Used only when the effective Anthropic base URL is empty/direct; CLIProxy Anthropic calls continue using `ANTHROPIC_API_KEY` (`cpx-*`). |
| `OPENAI_API_KEY` | OpenAI | If using `openai` provider; optional otherwise for OpenAI image generation, STT, and OpenAI-backed tools |
| `GEMINI_API_KEY` | Google Gemini | Optional; enables Gemini image generation, Gemini TTS, and Gemini attachment extraction |
| `OPENROUTER_API_KEY` | OpenRouter | If using `openrouter` provider |
| `EMBEDDING_API_KEY` | Memory embeddings | Optional; enables semantic memory/skill search. Holds the embedder's key for any cloud provider (OpenAI, Voyage, Cohere, Gemini). Keep separate from CLIProxy `OPENAI_API_KEY` values. Not needed for the `local` provider. |
| `EMBEDDING_PROVIDER` | Memory embeddings | Embedding backend: `openai` (any OpenAI-compatible endpoint incl. Voyage; default), `cohere` (native embed-v4), `gemini` (native embedding-001), or `local` (in-process sentence-transformers, e.g. granite; needs the local-rag extra). |
| `EMBEDDING_BASE_URL` | Memory embeddings | Optional custom `/v1` base URL for the `openai` provider (e.g. `https://api.voyageai.com/v1`, or a local embed shim) |
| `EMBEDDING_MODEL` | Memory embeddings | Embedding model name; defaults to `text-embedding-3-small`. Its output width must match `EMBEDDING_DIMENSIONS`. |
| `EMBEDDING_DIMENSIONS` | Memory embeddings | Vector width of the memory index. Blank keeps the legacy 1536 slot. Set to the model's native or Matryoshka width (1024 for Cohere/Gemini/Voyage, 384 for granite). Changing it (or the embedding model/provider) on an existing deployment needs a re-embed: run `nymeria reembed` (the vec0 width is fixed at table creation). |
| `EMBEDDING_INPUT_TYPE` | Memory embeddings | Asymmetric query/document scheme for OpenAI-compatible embedders. `voyage` sends `input_type=query`/`document` for Voyage models. Blank for symmetric models; native cohere/gemini handle this internally. |
| `RAG_EMBED_TOOL_RESULTS` | Memory embeddings | Embed tool-result content as retrievable `tool` chunks so the agent can recall what tools returned. On by default; tool chunks are hard-deduped at ingest (canonical-JSON hash plus the semantic guard). |
| `RAG_RETRIEVAL_MODE` | Memory retrieval | `hybrid` (BM25 + vector, default) or `vector` (vector-only). Hybrid is the robust default: if the embedder underperforms or is misconfigured, BM25 still salvages the ranking. Vector-only typically scores a little higher with a strong embedder but returns nothing if embeddings fail. This is the server default; each user can override it for their own corpus in Settings > RAG (or via the per-user rag settings API / rag_settings tool). |
| `RAG_RERANK_ENABLED` | Memory reranking | Rerank `rag_search` candidates before truncating to the requested count. Off by default (a reranker adds latency to each lookup). Server default; each user can override it per account in Settings > RAG. |
| `RAG_RERANK_PROVIDER` | Memory reranking | Reranker backend when enabled: `llm` (the thread's own model, default, no extra key), `voyage`/`cohere`/`zeroentropy` (managed rerank API, needs `RAG_RERANK_API_KEY`), or `local` (sentence-transformers cross-encoder, e.g. Ettin; needs the local-rag extra). |
| `RAG_RERANK_MODEL` | Memory reranking | Reranker model id, e.g. `rerank-2.5`/`rerank-2.5-lite` (Voyage), `zerank-2` (ZeroEntropy), or a cross-encoder id for `local`. Ignored for `llm`. |
| `RAG_RERANK_API_KEY` | Memory reranking | Key for the managed reranker; may equal `EMBEDDING_API_KEY` when one vendor powers both (e.g. Voyage embed plus Voyage rerank). |
| `PERPLEXITY_API_KEY` | Perplexity | Used by `web_search_perplexity` tool (credential vault preferred) |
| `TAVILY_API_KEY` | Tavily | Used by `web_search_tavily` tool (credential vault preferred) |
| `EXA_API_KEY` | Exa | Used by `web_search_exa_ai` tool (credential vault preferred) |
| `FIRECRAWL_API_KEY` | Firecrawl | Used by `web_search_firecrawl` tool (credential vault preferred) |
| `BRAVE_API_KEY` | Brave | Used by `web_search_brave` tool (credential vault preferred) |
| `WOLFRAM_ALPHA_APP_ID` | Wolfram\|Alpha | Optional env fallback for `wolfram_alpha_query`; credential vault provider `wolfram_alpha` is preferred |
| `SEARXNG_BASE_URL` | SearXNG | Base URL for `web_search_searxng` (defaults to the bundled `http://searxng:8080` sidecar); credential vault provider `searxng` field `base_url` also works |
| `NASA_API_KEY` | NASA | Optional env fallback for `nasa_apod`; credential vault provider `nasa` is preferred |
| `OPENWEATHERMAP_API_KEY` | OpenWeatherMap | Optional env fallback for `openweathermap_*`; credential vault provider `openweathermap` is preferred |
| `NPM_REGISTRY_URL` | npm | Optional registry override for npm tools; credential vault provider `npm` can also provide `registry_url` and `token` |
| `CRYPTO_HMAC_SECRET` | Crypto | Optional env fallback for HMAC transform tools; credential vault provider `crypto` is preferred |
| `CRYPTO_SIGN_PRIVATE_KEY` | Crypto | Optional PEM private key fallback for signing transform tools |
| `CRYPTO_SIGN_PRIVATE_KEY_PASSPHRASE` | Crypto | Optional passphrase for encrypted signing private keys |
| `JWT_SECRET` | JWT | Optional HMAC secret fallback for JWT tools; credential vault provider `jwt` is preferred |
| `JWT_PRIVATE_KEY` | JWT | Optional PEM private key fallback for JWT signing |
| `JWT_PUBLIC_KEY` | JWT | Optional PEM public key fallback for JWT verification |
| `JWT_ALGORITHM` | JWT | Optional default JWT algorithm; defaults to `HS256` |
| `GITHUB_TOKEN` | GitHub | Optional env fallback for GitHub developer-platform tools; credential vault provider `github` is preferred |
| `GITHUB_API_BASE_URL` | GitHub | Optional GitHub API base URL; defaults to `https://api.github.com` |
| `GITLAB_TOKEN` | GitLab | Optional env fallback for GitLab developer-platform tools; credential vault provider `gitlab` is preferred |
| `GITLAB_BASE_URL` | GitLab | Optional GitLab API base URL or instance URL; defaults to `https://gitlab.com/api/v4` |
| `CIRCLECI_API_TOKEN` | CircleCI | Optional env fallback for CircleCI build tools; credential vault provider `circleci` is preferred |
| `CIRCLECI_BASE_URL` | CircleCI | Optional CircleCI API base URL override |
| `TRAVISCI_API_TOKEN` | Travis CI | Optional env fallback for Travis CI build tools; credential vault provider `travisci` is preferred |
| `TRAVISCI_BASE_URL` | Travis CI | Optional Travis CI API base URL override |
| `JENKINS_BASE_URL` | Jenkins | Optional Jenkins instance URL fallback; credential vault provider `jenkins` is preferred |
| `JENKINS_USERNAME` | Jenkins | Optional Jenkins username fallback |
| `JENKINS_API_TOKEN` | Jenkins | Optional Jenkins API token fallback |
| `DROPBOX_ACCESS_TOKEN` | Dropbox | Optional env fallback for Dropbox file tools; credential vault provider `dropbox` is preferred |
| `DROPBOX_API_BASE_URL` | Dropbox | Optional Dropbox API base URL override |
| `DROPBOX_CONTENT_BASE_URL` | Dropbox | Optional Dropbox content API base URL override |
| `NEXTCLOUD_WEBDAV_URL` | Nextcloud | Optional Nextcloud WebDAV URL fallback; credential vault provider `nextcloud` is preferred |
| `NEXTCLOUD_USERNAME` | Nextcloud | Optional Nextcloud username fallback |
| `NEXTCLOUD_PASSWORD` | Nextcloud | Optional Nextcloud password or app password fallback |
| `NEXTCLOUD_ACCESS_TOKEN` | Nextcloud | Optional Nextcloud OAuth access token fallback |
| `AWS_ACCESS_KEY_ID` | S3 / AWS service tools | Optional env fallback for S3 and AWS service tools; credential vault provider `s3` or `aws` is preferred |
| `AWS_SECRET_ACCESS_KEY` | S3 / AWS service tools | Optional AWS secret access key fallback |
| `AWS_SESSION_TOKEN` | S3 / AWS service tools | Optional temporary AWS session token fallback |
| `AWS_REGION` | S3 / AWS service tools | Optional AWS region fallback; defaults to `us-east-1` |
| `AWS_ENDPOINT_URL_S3` | S3 | Optional S3-compatible endpoint URL |
| `S3_FORCE_PATH_STYLE` | S3 | Optional path-style addressing toggle for S3-compatible providers |
| `CLEARBIT_API_KEY` | Clearbit | Optional env fallback for Clearbit enrichment tools; credential vault provider `clearbit` is preferred |
| `CLEARBIT_COMPANY_BASE_URL` | Clearbit | Optional Clearbit company API base URL override |
| `CLEARBIT_PERSON_BASE_URL` | Clearbit | Optional Clearbit person API base URL override |
| `CLEARBIT_AUTOCOMPLETE_BASE_URL` | Clearbit | Optional Clearbit autocomplete API base URL override |
| `UPLEAD_API_KEY` | Uplead | Optional env fallback for Uplead enrichment tools; credential vault provider `uplead` is preferred |
| `UPLEAD_BASE_URL` | Uplead | Optional Uplead API base URL override |
| `DROPCONTACT_API_KEY` | Dropcontact | Optional env fallback for Dropcontact tools; credential vault provider `dropcontact` is preferred |
| `DROPCONTACT_BASE_URL` | Dropcontact | Optional Dropcontact API base URL override |
| `HUMANTIC_API_KEY` | Humantic AI | Optional env fallback for Humantic AI tools; credential vault provider `humantic` is preferred |
| `HUMANTIC_BASE_URL` | Humantic AI | Optional Humantic AI API base URL override |
| `LONESCALE_API_KEY` | LoneScale | Optional env fallback for LoneScale tools; credential vault provider `lonescale` is preferred |
| `LONESCALE_BASE_URL` | LoneScale | Optional LoneScale API base URL override |
| `UPROC_EMAIL` | uProc | Optional uProc account email fallback; credential vault provider `uproc` is preferred |
| `UPROC_API_KEY` | uProc | Optional uProc API key fallback |
| `UPROC_BASE_URL` | uProc | Optional uProc API base URL override |
| `BITLY_TOKEN` | Bitly | Optional env fallback for Bitly tools; credential vault provider `bitly` is preferred |
| `BITLY_BASE_URL` | Bitly | Optional Bitly API base URL override |
| `BRANDFETCH_API_KEY` | Brandfetch | Optional env fallback for Brandfetch tools; credential vault provider `brandfetch` is preferred |
| `BRANDFETCH_BASE_URL` | Brandfetch | Optional Brandfetch API base URL override |
| `MARKETSTACK_API_KEY` | Marketstack | Optional env fallback for Marketstack tools; credential vault provider `marketstack` is preferred |
| `MARKETSTACK_BASE_URL` | Marketstack | Optional Marketstack API base URL override |
| `DEEPL_API_KEY` | DeepL | Optional env fallback for DeepL tools; credential vault provider `deepl` is preferred |
| `DEEPL_API_PLAN` | DeepL | Optional plan selector, `pro` or `free`; defaults to `pro` |
| `DEEPL_BASE_URL` | DeepL | Optional DeepL API base URL override |
| `LINGVANEX_API_KEY` | LingvaNex | Optional env fallback for LingvaNex tools; credential vault provider `lingvanex` is preferred |
| `LINGVANEX_BASE_URL` | LingvaNex | Optional LingvaNex API base URL override |
| `APITEMPLATE_API_KEY` | APITemplate | Optional env fallback for APITemplate tools; credential vault provider `apitemplate` is preferred |
| `APITEMPLATE_BASE_URL` | APITemplate | Optional APITemplate API base URL override |
| `ONESIMPLE_API_TOKEN` | One Simple API | Optional env fallback for One Simple API tools; credential vault provider `onesimple` is preferred |
| `ONESIMPLE_BASE_URL` | One Simple API | Optional One Simple API base URL override |
| `ERPNEXT_API_KEY` | ERPNext | Optional ERPNext API key fallback; credential vault provider `erpnext` is preferred |
| `ERPNEXT_API_SECRET` | ERPNext | Optional ERPNext API secret fallback |
| `ERPNEXT_BASE_URL` | ERPNext | Optional ERPNext site base URL override |
| `ERPNEXT_SUBDOMAIN` | ERPNext | Optional ERPNext cloud subdomain fallback |
| `ERPNEXT_CLOUD_DOMAIN` | ERPNext | Optional ERPNext cloud domain fallback |
| `ODOO_URL` | Odoo | Optional Odoo site URL fallback; credential vault provider `odoo` is preferred |
| `ODOO_USERNAME` | Odoo | Optional Odoo username fallback |
| `ODOO_PASSWORD` | Odoo | Optional Odoo password or API key fallback |
| `ODOO_DATABASE` | Odoo | Optional Odoo database name fallback |
| `INVOICENINJA_API_TOKEN` | Invoice Ninja | Optional Invoice Ninja API token fallback; credential vault provider `invoiceninja` is preferred |
| `INVOICENINJA_SECRET` | Invoice Ninja | Optional Invoice Ninja v5 API secret fallback |
| `INVOICENINJA_BASE_URL` | Invoice Ninja | Optional Invoice Ninja API base URL override |
| `INVOICENINJA_API_VERSION` | Invoice Ninja | Optional Invoice Ninja API version, `v4` or `v5` |
| `DEMIO_API_KEY` | Demio | Optional Demio API key fallback; credential vault provider `demio` is preferred |
| `DEMIO_API_SECRET` | Demio | Optional Demio API secret fallback |
| `DEMIO_BASE_URL` | Demio | Optional Demio API base URL override |
| `ZOOM_ACCESS_TOKEN` | Zoom | Optional Zoom OAuth or bearer token fallback; credential vault provider `zoom` is preferred |
| `ZOOM_BASE_URL` | Zoom | Optional Zoom API base URL override |
| `GOTOWEBINAR_ACCESS_TOKEN` | GoToWebinar | Optional GoToWebinar OAuth token fallback; credential vault provider `gotowebinar` is preferred |
| `GOTOWEBINAR_ACCOUNT_KEY` | GoToWebinar | Optional GoToWebinar account key fallback |
| `GOTOWEBINAR_ORGANIZER_KEY` | GoToWebinar | Optional GoToWebinar organizer key fallback |
| `GOTOWEBINAR_BASE_URL` | GoToWebinar | Optional GoToWebinar API base URL override |
| `TODOIST_API_KEY` | Todoist | Optional env fallback for Todoist tools; credential vault provider `todoist` is preferred |
| `TODOIST_BASE_URL` | Todoist | Optional Todoist API base URL override |
| `TRELLO_API_KEY` | Trello | Optional env fallback for Trello tools; credential vault provider `trello` is preferred |
| `TRELLO_API_TOKEN` | Trello | Optional env fallback for Trello tools; credential vault provider `trello` is preferred |
| `TRELLO_BASE_URL` | Trello | Optional Trello API base URL override |
| `RAINDROP_ACCESS_TOKEN` | Raindrop | Optional Raindrop access token fallback; credential vault provider `raindrop` is preferred |
| `RAINDROP_BASE_URL` | Raindrop | Optional Raindrop API base URL override |
| `YOURLS_URL` | YOURLS | Optional YOURLS site or API URL fallback; credential vault provider `yourls` is preferred |
| `YOURLS_SIGNATURE` | YOURLS | Optional YOURLS signature token fallback |
| `YOURLS_USERNAME` | YOURLS | Optional YOURLS username fallback |
| `YOURLS_PASSWORD` | YOURLS | Optional YOURLS password fallback |
| `ASANA_ACCESS_TOKEN` | Asana | Optional env fallback for Asana tools; credential vault provider `asana` is preferred |
| `ASANA_BASE_URL` | Asana | Optional Asana API base URL override |
| `LINEAR_API_KEY` | Linear | Optional env fallback for Linear tools; credential vault provider `linear` is preferred |
| `LINEAR_API_URL` | Linear | Optional Linear GraphQL API URL override |
| `GRAPHQL_ENDPOINT` | GraphQL | Optional generic GraphQL endpoint fallback; credential vault provider `graphql` is preferred |
| `GRAPHQL_BEARER_TOKEN` | GraphQL | Optional generic GraphQL bearer token fallback |
| `GRAPHQL_API_KEY` | GraphQL | Optional generic GraphQL API key fallback |
| `GRAPHQL_API_KEY_HEADER` | GraphQL | Optional generic GraphQL API key header name; defaults to `x-api-key` |
| `GRAPHQL_HEADERS_JSON` | GraphQL | Optional generic GraphQL default headers as a JSON object |
| `TOTP_SECRET` | TOTP | Optional base32 TOTP secret fallback; credential vault provider `totp` is preferred |
| `JIRA_EMAIL` | Jira | Optional env fallback email for Jira basic auth; credential vault provider `jira` is preferred |
| `JIRA_API_TOKEN` | Jira | Optional env fallback token for Jira basic auth |
| `JIRA_ACCESS_TOKEN` | Jira | Optional env fallback bearer token for Jira |
| `JIRA_BASE_URL` | Jira | Jira Cloud site base URL, such as `https://example.atlassian.net` |
| `CLICKUP_ACCESS_TOKEN` | ClickUp | Optional env fallback for ClickUp tools; credential vault provider `clickup` is preferred |
| `CLICKUP_BASE_URL` | ClickUp | Optional ClickUp API base URL override |
| `MONDAY_API_TOKEN` | Monday | Optional env fallback for Monday tools; credential vault provider `monday` is preferred |
| `MONDAY_API_URL` | Monday | Optional Monday GraphQL API URL override |
| `TAIGA_AUTH_TOKEN` | Taiga | Optional env fallback auth token for Taiga tools; credential vault provider `taiga` is preferred |
| `TAIGA_USERNAME` | Taiga | Optional Taiga username fallback for login-based auth |
| `TAIGA_PASSWORD` | Taiga | Optional Taiga password fallback for login-based auth |
| `TAIGA_BASE_URL` | Taiga | Optional Taiga API base URL or instance root override |
| `WEKAN_BASE_URL` | Wekan | Wekan instance root URL; credential vault provider `wekan` is preferred |
| `WEKAN_TOKEN` | Wekan | Optional env fallback session token for Wekan tools |
| `WEKAN_USERNAME` | Wekan | Optional Wekan username fallback for login-based auth |
| `WEKAN_PASSWORD` | Wekan | Optional Wekan password fallback for login-based auth |
| `SLACK_BOT_TOKEN` | Slack | Optional env fallback for Slack tools; credential vault provider `slack` is preferred |
| `SLACK_ACCESS_TOKEN` | Slack | Optional alternate env fallback for Slack tools |
| `SLACK_BASE_URL` | Slack | Optional Slack Web API base URL override |
| `NOTION_API_KEY` | Notion | Optional env fallback for Notion tools; credential vault provider `notion` is preferred |
| `NOTION_VERSION` | Notion | Optional Notion API version override |
| `NOTION_BASE_URL` | Notion | Optional Notion API base URL override |
| `AIRTABLE_ACCESS_TOKEN` | Airtable | Optional env fallback for Airtable tools; credential vault provider `airtable` is preferred |
| `AIRTABLE_API_KEY` | Airtable | Optional legacy Airtable API key fallback |
| `AIRTABLE_BASE_URL` | Airtable | Optional Airtable API base URL override |
| `HUBSPOT_ACCESS_TOKEN` | HubSpot | Optional env fallback for HubSpot tools; credential vault provider `hubspot` is preferred |
| `HUBSPOT_BASE_URL` | HubSpot | Optional HubSpot API base URL override |
| `ZENDESK_EMAIL` | Zendesk | Optional env fallback email for Zendesk API-token auth |
| `ZENDESK_API_TOKEN` | Zendesk | Optional env fallback token for Zendesk API-token auth |
| `ZENDESK_ACCESS_TOKEN` | Zendesk | Optional env fallback bearer token for Zendesk |
| `ZENDESK_SUBDOMAIN` | Zendesk | Optional Zendesk subdomain fallback |
| `ZENDESK_BASE_URL` | Zendesk | Optional Zendesk API base URL override |
| `MAILCHIMP_API_KEY` | Mailchimp | Optional env fallback for Mailchimp tools; credential vault provider `mailchimp` is preferred |
| `MAILCHIMP_ACCESS_TOKEN` | Mailchimp | Optional Mailchimp OAuth token fallback |
| `MAILCHIMP_SERVER_PREFIX` | Mailchimp | Optional Mailchimp server prefix such as `us21` |
| `MAILCHIMP_BASE_URL` | Mailchimp | Optional Mailchimp Marketing API base URL override |
| `FRESHDESK_API_KEY` | Freshdesk | Optional env fallback for Freshdesk tools; credential vault provider `freshdesk` is preferred |
| `FRESHDESK_DOMAIN` | Freshdesk | Optional Freshdesk subdomain fallback |
| `FRESHDESK_BASE_URL` | Freshdesk | Optional Freshdesk API base URL override |
| `FRESHSERVICE_API_KEY` | Freshservice | Optional env fallback for Freshservice tools; credential vault provider `freshservice` is preferred |
| `FRESHSERVICE_DOMAIN` | Freshservice | Optional Freshservice subdomain fallback |
| `FRESHSERVICE_BASE_URL` | Freshservice | Optional Freshservice API base URL override |
| `SERVICENOW_BASE_URL` | ServiceNow | Optional ServiceNow API base URL override |
| `SERVICENOW_INSTANCE` | ServiceNow | Optional ServiceNow instance subdomain fallback |
| `SERVICENOW_ACCESS_TOKEN` | ServiceNow | Optional ServiceNow bearer token fallback |
| `SERVICENOW_USERNAME` | ServiceNow | Optional ServiceNow basic-auth username fallback |
| `SERVICENOW_PASSWORD` | ServiceNow | Optional ServiceNow basic-auth password fallback |
| `ZAMMAD_BASE_URL` | Zammad | Optional Zammad API base URL fallback |
| `ZAMMAD_TOKEN` | Zammad | Optional Zammad token fallback |
| `ZAMMAD_USERNAME` | Zammad | Optional Zammad basic-auth username fallback |
| `ZAMMAD_PASSWORD` | Zammad | Optional Zammad basic-auth password fallback |
| `HELPSCOUT_ACCESS_TOKEN` | Help Scout | Optional env fallback for Help Scout tools; credential vault provider `helpscout` is preferred |
| `HELPSCOUT_BASE_URL` | Help Scout | Optional Help Scout API base URL override |
| `INTERCOM_ACCESS_TOKEN` | Intercom | Optional env fallback for Intercom tools; credential vault provider `intercom` is preferred |
| `INTERCOM_BASE_URL` | Intercom | Optional Intercom API base URL override |
| `INTERCOM_VERSION` | Intercom | Optional Intercom API version header override |
| `SALESFORCE_INSTANCE_URL` | Salesforce | Optional Salesforce instance URL fallback; credential vault provider `salesforce` is preferred |
| `SALESFORCE_ACCESS_TOKEN` | Salesforce | Optional Salesforce OAuth access token fallback |
| `SALESFORCE_BASE_URL` | Salesforce | Optional Salesforce API base URL override |
| `SALESFORCE_API_VERSION` | Salesforce | Optional Salesforce REST API version override |
| `ZOHO_CRM_ACCESS_TOKEN` | Zoho CRM | Optional Zoho CRM OAuth access token fallback; credential vault provider `zoho_crm` is preferred |
| `ZOHO_CRM_API_DOMAIN` | Zoho CRM | Optional Zoho CRM API domain fallback |
| `ZOHO_CRM_BASE_URL` | Zoho CRM | Optional Zoho CRM API base URL override |
| `FRESHWORKS_CRM_API_KEY` | Freshworks CRM | Optional Freshworks CRM API key fallback; credential vault provider `freshworks_crm` is preferred |
| `FRESHWORKS_CRM_DOMAIN` | Freshworks CRM | Optional Freshworks CRM account domain fallback |
| `FRESHWORKS_CRM_BASE_URL` | Freshworks CRM | Optional Freshworks CRM API base URL override |
| `SALESMATE_SESSION_TOKEN` | Salesmate | Optional Salesmate session token fallback; credential vault provider `salesmate` is preferred |
| `SALESMATE_LINK_NAME` | Salesmate | Optional Salesmate link name fallback |
| `SALESMATE_BASE_URL` | Salesmate | Optional Salesmate API base URL override |
| `PIPEDRIVE_API_TOKEN` | Pipedrive | Optional env fallback for Pipedrive API-token auth; credential vault provider `pipedrive` is preferred |
| `PIPEDRIVE_ACCESS_TOKEN` | Pipedrive | Optional Pipedrive OAuth bearer token fallback |
| `PIPEDRIVE_BASE_URL` | Pipedrive | Optional Pipedrive API base URL override |
| `TWILIO_ACCOUNT_SID` | Twilio | Optional Twilio account SID fallback; credential vault provider `twilio` is preferred |
| `TWILIO_AUTH_TOKEN` | Twilio | Optional Twilio auth token or API key secret fallback |
| `TWILIO_API_KEY_SID` | Twilio | Optional Twilio API key SID fallback |
| `TWILIO_BASE_URL` | Twilio | Optional Twilio API base URL override |
| `SENDGRID_API_KEY` | SendGrid | Optional env fallback for SendGrid tools; credential vault provider `sendgrid` is preferred |
| `SENDGRID_BASE_URL` | SendGrid | Optional SendGrid API base URL override |
| `MAILGUN_API_KEY` | Mailgun | Optional env fallback for Mailgun tools; credential vault provider `mailgun` is preferred |
| `MAILGUN_DOMAIN` | Mailgun | Optional Mailgun sending domain fallback |
| `MAILGUN_BASE_URL` | Mailgun | Optional Mailgun API base URL override |
| `BREVO_API_KEY` | Brevo | Optional env fallback for Brevo tools; credential vault provider `brevo` is preferred |
| `BREVO_BASE_URL` | Brevo | Optional Brevo API base URL override |
| `MAILJET_API_KEY` | Mailjet | Optional Mailjet email API key fallback; credential vault provider `mailjet` is preferred |
| `MAILJET_SECRET_KEY` | Mailjet | Optional Mailjet email secret key fallback |
| `MAILJET_SMS_TOKEN` | Mailjet | Optional Mailjet SMS token fallback |
| `MAILJET_BASE_URL` | Mailjet | Optional Mailjet API base URL override |
| `MANDRILL_API_KEY` | Mandrill | Optional Mandrill / Mailchimp Transactional API key fallback; credential vault provider `mandrill` is preferred |
| `MANDRILL_BASE_URL` | Mandrill | Optional Mandrill API base URL override |
| `MESSAGEBIRD_ACCESS_KEY` | MessageBird | Optional MessageBird access key fallback; credential vault provider `messagebird` is preferred |
| `MESSAGEBIRD_BASE_URL` | MessageBird | Optional MessageBird API base URL override |
| `MOCEAN_API_KEY` | Mocean | Optional Mocean API key fallback; credential vault provider `mocean` is preferred |
| `MOCEAN_API_SECRET` | Mocean | Optional Mocean API secret fallback |
| `MOCEAN_BASE_URL` | Mocean | Optional Mocean API base URL override |
| `MSG91_AUTH_KEY` | MSG91 | Optional MSG91 authentication key fallback; credential vault provider `msg91` is preferred |
| `MSG91_BASE_URL` | MSG91 | Optional MSG91 API base URL override |
| `PLIVO_AUTH_ID` | Plivo | Optional Plivo auth ID fallback; credential vault provider `plivo` is preferred |
| `PLIVO_AUTH_TOKEN` | Plivo | Optional Plivo auth token fallback |
| `PLIVO_BASE_URL` | Plivo | Optional Plivo API base URL override |
| `VONAGE_API_KEY` | Vonage | Optional Vonage API key fallback; credential vault provider `vonage` is preferred |
| `VONAGE_API_SECRET` | Vonage | Optional Vonage API secret fallback |
| `VONAGE_BASE_URL` | Vonage | Optional Vonage REST API base URL override |
| `SEVEN_API_KEY` | seven.io | Optional seven.io API key fallback; credential vault provider `seven` is preferred |
| `SEVEN_BASE_URL` | seven.io | Optional seven.io API base URL override |
| `STRIPE_SECRET_KEY` | Stripe | Optional env fallback for Stripe tools; credential vault provider `stripe` is preferred |
| `STRIPE_BASE_URL` | Stripe | Optional Stripe API base URL override |
| `SHOPIFY_SHOP` | Shopify | Optional Shopify shop subdomain or myshopify.com host fallback; credential vault provider `shopify` is preferred |
| `SHOPIFY_ACCESS_TOKEN` | Shopify | Optional Shopify Admin API access token fallback |
| `SHOPIFY_API_KEY` | Shopify | Optional legacy Shopify API key fallback |
| `SHOPIFY_PASSWORD` | Shopify | Optional legacy Shopify Admin API password fallback |
| `SHOPIFY_API_VERSION` | Shopify | Optional Shopify Admin REST API version override |
| `SHOPIFY_BASE_URL` | Shopify | Optional Shopify Admin REST API base URL override |
| `WOOCOMMERCE_URL` | WooCommerce | Optional WooCommerce site URL fallback; credential vault provider `woocommerce` is preferred |
| `WOOCOMMERCE_BASE_URL` | WooCommerce | Optional WooCommerce REST API base URL override |
| `WOOCOMMERCE_CONSUMER_KEY` | WooCommerce | Optional WooCommerce consumer key fallback |
| `WOOCOMMERCE_CONSUMER_SECRET` | WooCommerce | Optional WooCommerce consumer secret fallback |
| `CHARGEBEE_API_KEY` | Chargebee | Optional env fallback for Chargebee tools; credential vault provider `chargebee` is preferred |
| `CHARGEBEE_SITE` | Chargebee | Optional Chargebee site subdomain fallback |
| `CHARGEBEE_BASE_URL` | Chargebee | Optional Chargebee API base URL override |
| `PADDLE_VENDOR_ID` | Paddle | Optional Paddle vendor ID fallback; credential vault provider `paddle` is preferred |
| `PADDLE_VENDOR_AUTH_CODE` | Paddle | Optional Paddle vendor auth code fallback |
| `PADDLE_SANDBOX` | Paddle | Use Paddle sandbox vendor API |
| `PADDLE_BASE_URL` | Paddle | Optional Paddle vendor API base URL override |
| `PROFITWELL_API_TOKEN` | ProfitWell | Optional env fallback for ProfitWell tools; credential vault provider `profitwell` is preferred |
| `PROFITWELL_BASE_URL` | ProfitWell | Optional ProfitWell API base URL override |
| `TAPFILIATE_API_KEY` | Tapfiliate | Optional env fallback for Tapfiliate tools; credential vault provider `tapfiliate` is preferred |
| `TAPFILIATE_BASE_URL` | Tapfiliate | Optional Tapfiliate API base URL override |
| `PUSHBULLET_ACCESS_TOKEN` | Pushbullet | Optional env fallback for Pushbullet tools; credential vault provider `pushbullet` is preferred |
| `PUSHBULLET_BASE_URL` | Pushbullet | Optional Pushbullet API base URL override |
| `PUSHCUT_API_KEY` | Pushcut | Optional env fallback for Pushcut tools; credential vault provider `pushcut` is preferred |
| `PUSHCUT_BASE_URL` | Pushcut | Optional Pushcut API base URL override |
| `GOTIFY_BASE_URL` | Gotify | Optional Gotify server base URL fallback; credential vault provider `gotify` is preferred |
| `GOTIFY_APP_TOKEN` | Gotify | Optional Gotify application token fallback for sending messages |
| `GOTIFY_CLIENT_TOKEN` | Gotify | Optional Gotify client token fallback for listing/deleting messages |
| `PUSHOVER_API_TOKEN` | Pushover | Optional env fallback for Pushover app API token; credential vault provider `pushover` is preferred |
| `PUSHOVER_USER_KEY` | Pushover | Optional Pushover user or group key fallback |
| `PUSHOVER_BASE_URL` | Pushover | Optional Pushover API base URL override |
| `SIGNL4_TEAM_SECRET` | SIGNL4 | Optional SIGNL4 team secret fallback; credential vault provider `signl4` is preferred |
| `SIGNL4_WEBHOOK_URL` | SIGNL4 | Optional full SIGNL4 webhook URL fallback |
| `SIGNL4_BASE_URL` | SIGNL4 | Optional SIGNL4 webhook base URL override |
| `WORDPRESS_URL` | WordPress | Optional WordPress site URL fallback; credential vault provider `wordpress` is preferred |
| `WORDPRESS_USERNAME` | WordPress | Optional WordPress username fallback |
| `WORDPRESS_PASSWORD` | WordPress | Optional WordPress application password fallback |
| `STRAPI_URL` | Strapi | Optional Strapi API base URL fallback; credential vault provider `strapi` is preferred |
| `STRAPI_API_TOKEN` | Strapi | Optional Strapi API token fallback |
| `STRAPI_EMAIL` | Strapi | Optional Strapi local-auth email fallback |
| `STRAPI_PASSWORD` | Strapi | Optional Strapi local-auth password fallback |
| `STRAPI_API_VERSION` | Strapi | Optional Strapi REST API version hint |
| `CONTENTFUL_SPACE_ID` | Contentful | Optional Contentful space ID fallback; credential vault provider `contentful` is preferred |
| `CONTENTFUL_DELIVERY_TOKEN` | Contentful | Optional Contentful delivery access token fallback |
| `CONTENTFUL_PREVIEW_TOKEN` | Contentful | Optional Contentful preview access token fallback |
| `CONTENTFUL_BASE_URL` | Contentful | Optional Contentful Delivery API base URL override |
| `CONTENTFUL_PREVIEW_BASE_URL` | Contentful | Optional Contentful Preview API base URL override |
| `GHOST_URL` | Ghost | Optional Ghost site URL fallback; credential vault provider `ghost` is preferred |
| `GHOST_CONTENT_API_KEY` | Ghost | Optional Ghost Content API key fallback |
| `GHOST_ADMIN_API_KEY` | Ghost | Optional Ghost Admin API key fallback |
| `GHOST_API_VERSION` | Ghost | Optional Ghost API version header override |
| `STORYBLOK_CONTENT_TOKEN` | Storyblok | Optional Storyblok Content API token fallback; credential vault provider `storyblok` is preferred |
| `STORYBLOK_MANAGEMENT_TOKEN` | Storyblok | Optional Storyblok Management API token fallback |
| `STORYBLOK_SPACE_ID` | Storyblok | Optional Storyblok space ID fallback for Management API tools |
| `STORYBLOK_CONTENT_BASE_URL` | Storyblok | Optional Storyblok Content API base URL override |
| `STORYBLOK_MANAGEMENT_BASE_URL` | Storyblok | Optional Storyblok Management API base URL override |
| `NETLIFY_ACCESS_TOKEN` | Netlify | Optional Netlify access token fallback; credential vault provider `netlify` is preferred |
| `NETLIFY_BASE_URL` | Netlify | Optional Netlify API base URL override |
| `UPTIMEROBOT_API_KEY` | UptimeRobot | Optional UptimeRobot API key fallback; credential vault provider `uptimerobot` is preferred |
| `UPTIMEROBOT_BASE_URL` | UptimeRobot | Optional UptimeRobot API base URL override |
| `PAGERDUTY_API_TOKEN` | PagerDuty | Optional PagerDuty API token fallback; credential vault provider `pagerduty` is preferred |
| `PAGERDUTY_FROM_EMAIL` | PagerDuty | Optional PagerDuty From email fallback for write operations |
| `PAGERDUTY_BASE_URL` | PagerDuty | Optional PagerDuty API base URL override |
| `SENTRY_AUTH_TOKEN` | Sentry | Optional Sentry auth token fallback; credential vault provider `sentry` is preferred |
| `SENTRY_BASE_URL` | Sentry | Optional Sentry API base URL override |
| `CLOUDFLARE_API_TOKEN` | Cloudflare | Optional Cloudflare API token fallback; credential vault provider `cloudflare` is preferred |
| `CLOUDFLARE_BASE_URL` | Cloudflare | Optional Cloudflare API base URL override |
| `GRAFANA_API_TOKEN` | Grafana | Optional Grafana API token fallback; credential vault provider `grafana` is preferred |
| `GRAFANA_BASE_URL` | Grafana | Optional Grafana base URL fallback |
| `METABASE_BASE_URL` | Metabase | Optional Metabase base URL fallback; credential vault provider `metabase` is preferred |
| `METABASE_SESSION_TOKEN` | Metabase | Optional Metabase session token fallback |
| `METABASE_API_KEY` | Metabase | Optional Metabase API key fallback |
| `METABASE_USERNAME` | Metabase | Optional Metabase username fallback |
| `METABASE_PASSWORD` | Metabase | Optional Metabase password fallback |
| `ELASTICSEARCH_BASE_URL` | Elasticsearch | Optional Elasticsearch base URL fallback; credential vault provider `elasticsearch` is preferred |
| `ELASTICSEARCH_API_KEY` | Elasticsearch | Optional Elasticsearch API key fallback |
| `ELASTICSEARCH_BEARER_TOKEN` | Elasticsearch | Optional Elasticsearch bearer token fallback |
| `ELASTICSEARCH_USERNAME` | Elasticsearch | Optional Elasticsearch username fallback |
| `ELASTICSEARCH_PASSWORD` | Elasticsearch | Optional Elasticsearch password fallback |
| `ELASTICSEARCH_IGNORE_SSL_ISSUES` | Elasticsearch | Optional flag to skip Elasticsearch SSL verification |
| `SPLUNK_BASE_URL` | Splunk | Optional Splunk management API base URL fallback; credential vault provider `splunk` is preferred |
| `SPLUNK_AUTH_TOKEN` | Splunk | Optional Splunk auth token fallback |
| `SPLUNK_ALLOW_UNAUTHORIZED_CERTS` | Splunk | Optional flag to allow self-signed Splunk certificates |
| `URLSCAN_API_KEY` | urlscan.io | Optional urlscan.io API key fallback; credential vault provider `urlscan` is preferred |
| `URLSCAN_BASE_URL` | urlscan.io | Optional urlscan.io API base URL override |
| `HUNTER_API_KEY` | Hunter | Optional Hunter API key fallback; credential vault provider `hunter` is preferred |
| `HUNTER_BASE_URL` | Hunter | Optional Hunter API base URL override |
| `MAILCHECK_API_KEY` | Mailcheck | Optional Mailcheck API key fallback; credential vault provider `mailcheck` is preferred |
| `MAILCHECK_BASE_URL` | Mailcheck | Optional Mailcheck API base URL override |
| `PEEKALINK_API_KEY` | Peekalink | Optional Peekalink API key fallback; credential vault provider `peekalink` is preferred |
| `PEEKALINK_BASE_URL` | Peekalink | Optional Peekalink API base URL override |
| `JINA_API_KEY` | Jina AI | Optional Jina AI API key fallback; credential vault provider `jina` is preferred |
| `JINA_READER_BASE_URL` | Jina AI | Optional Jina Reader API base URL override |
| `JINA_SEARCH_BASE_URL` | Jina AI | Optional Jina Search API base URL override |
| `JINA_DEEPSEARCH_BASE_URL` | Jina AI | Optional Jina DeepSearch API base URL override |
| `MISP_BASE_URL` | MISP | Optional MISP base URL fallback; credential vault provider `misp` is preferred |
| `MISP_API_KEY` | MISP | Optional MISP API key fallback |
| `MISP_ALLOW_UNAUTHORIZED_CERTS` | MISP | Deprecated / ignored. TLS verification is always enforced |
| `THEHIVE_BASE_URL` | TheHive | Optional TheHive base URL fallback; credential vault provider `thehive` is preferred |
| `THEHIVE_API_KEY` | TheHive | Optional TheHive API key fallback |
| `THEHIVE_API_VERSION` | TheHive | Optional TheHive API version hint |
| `THEHIVE_ALLOW_UNAUTHORIZED_CERTS` | TheHive | Deprecated / ignored. TLS verification is always enforced |
| `SECURITYSCORECARD_API_KEY` | SecurityScorecard | Optional SecurityScorecard API key fallback; credential vault provider `securityscorecard` is preferred |
| `SECURITYSCORECARD_BASE_URL` | SecurityScorecard | Optional SecurityScorecard API base URL override |
| `ELASTIC_SECURITY_BASE_URL` | Elastic Security | Optional Elastic Security Kibana base URL fallback; credential vault provider `elastic_security` is preferred |
| `ELASTIC_SECURITY_API_KEY` | Elastic Security | Optional Elastic Security API key fallback |
| `ELASTIC_SECURITY_USERNAME` | Elastic Security | Optional Elastic Security basic-auth username fallback |
| `ELASTIC_SECURITY_PASSWORD` | Elastic Security | Optional Elastic Security basic-auth password fallback |
| `BASEROW_API_TOKEN` | Baserow | Optional Baserow API or database token fallback; credential vault provider `baserow` is preferred |
| `BASEROW_BASE_URL` | Baserow | Optional Baserow API base URL override |
| `SUPABASE_URL` | Supabase | Optional Supabase project URL fallback; credential vault provider `supabase` is preferred |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase | Optional Supabase service role key fallback |
| `SUPABASE_API_KEY` | Supabase | Optional Supabase anon/API key fallback |
| `SUPABASE_BASE_URL` | Supabase | Optional Supabase REST API base URL override |
| `QUICKBASE_HOSTNAME` | Quickbase | Optional Quickbase realm hostname fallback; credential vault provider `quickbase` is preferred |
| `QUICKBASE_USER_TOKEN` | Quickbase | Optional Quickbase user token fallback |
| `QUICKBASE_BASE_URL` | Quickbase | Optional Quickbase API base URL override |
| `SEATABLE_API_TOKEN` | SeaTable | Optional SeaTable API token fallback; credential vault provider `seatable` is preferred |
| `SEATABLE_BASE_URL` | SeaTable | Optional SeaTable API base URL override |
| `STACKBY_API_KEY` | Stackby | Optional Stackby API key fallback; credential vault provider `stackby` is preferred |
| `STACKBY_BASE_URL` | Stackby | Optional Stackby API base URL override |
| `NOCODB_API_TOKEN` | NocoDB | Optional NocoDB API or user token fallback; credential vault provider `nocodb` is preferred |
| `NOCODB_BASE_URL` | NocoDB | Optional NocoDB API base URL override |
| `NOCODB_AUTH_HEADER` | NocoDB | Optional NocoDB auth header, `xc-token` for API tokens or `xc-auth` for user tokens |
| `CODA_API_TOKEN` | Coda | Optional Coda API token fallback; credential vault provider `coda` is preferred |
| `CODA_BASE_URL` | Coda | Optional Coda API base URL override |
| `GRIST_API_KEY` | Grist | Optional Grist API key fallback; credential vault provider `grist` is preferred |
| `GRIST_BASE_URL` | Grist | Optional Grist API base URL override |
| `ADALO_API_KEY` | Adalo | Optional Adalo API key fallback; credential vault provider `adalo` is preferred |
| `ADALO_APP_ID` | Adalo | Optional Adalo app ID fallback |
| `ADALO_BASE_URL` | Adalo | Optional Adalo app API base URL override |
| `BUBBLE_API_TOKEN` | Bubble | Optional Bubble Data API token fallback; credential vault provider `bubble` is preferred |
| `BUBBLE_APP_NAME` | Bubble | Optional Bubble app name fallback |
| `BUBBLE_ENVIRONMENT` | Bubble | Optional Bubble environment, `live` or `development` |
| `BUBBLE_DOMAIN` | Bubble | Optional Bubble custom/self-hosted domain fallback |
| `BUBBLE_BASE_URL` | Bubble | Optional Bubble API base URL override |
| `COCKPIT_BASE_URL` | Cockpit | Cockpit site or API base URL fallback |
| `COCKPIT_ACCESS_TOKEN` | Cockpit | Optional Cockpit access token fallback; credential vault provider `cockpit` is preferred |
| `TELEGRAM_API_BASE_URL` | Telegram | Optional Telegram Bot API base URL override |
| `WHATSAPP_ACCESS_TOKEN` | WhatsApp Business Cloud | Optional WhatsApp access token fallback; credential vault provider `whatsapp` is preferred |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | WhatsApp Business Cloud | Optional WhatsApp business account ID fallback |
| `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp Business Cloud | Optional WhatsApp sender phone number ID fallback |
| `WHATSAPP_BASE_URL` | WhatsApp Business Cloud | Optional WhatsApp Graph API base URL override |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | WhatsApp Business Cloud | Verification token for `GET /integrations/whatsapp/webhook` challenge-response setup |
| `WHATSAPP_APP_SECRET` | WhatsApp Business Cloud | Required Meta app secret for `X-Hub-Signature-256` webhook verification |
| `WHATSAPP_SHOW_TOOL_EVENTS` | WhatsApp Business Cloud | `true` posts compact tool-call/result messages in WhatsApp replies; default `false` |
| `DISCORD_BASE_URL` | Discord | Optional Discord REST API base URL override |
| `TEAMS_BOT_APP_ID` | Microsoft Teams | Bot Framework app ID for Teams webhook replies |
| `TEAMS_BOT_APP_PASSWORD` | Microsoft Teams | Bot Framework client secret for Teams webhook replies |
| `TEAMS_BOT_TENANT_ID` | Microsoft Teams | Optional tenant ID used during Azure setup |
| `TEAMS_BOT_RESPOND_MODE` | Microsoft Teams | Bot channel behavior: `mention` (personal chats and @mentions) or `all`; default `mention` |
| `TEAMS_BOT_VALIDATE_AUTH` | Microsoft Teams | Deprecated and ignored; Bot Framework bearer-token validation is always enforced |
| `TEAMS_BOT_SHOW_TOOL_EVENTS` | Microsoft Teams | `true` posts compact tool-call/result messages in Teams replies; default `false` |
| `TEAMS_BOT_TOKEN_URL` | Microsoft Teams | Bot Framework OAuth token URL override |
| `TEAMS_BOT_OPENID_CONFIG_URL` | Microsoft Teams | Bot Framework OpenID metadata URL override for webhook JWT validation |
| `GOOGLE_BOOKS_API_KEY` | Google Books | Optional Google Books API key fallback; credential vault provider `google_books` is preferred |
| `GOOGLE_BOOKS_BASE_URL` | Google Books | Optional Google Books API base URL override |
| `YOUTUBE_API_KEY` | YouTube Data API | YouTube Data API key fallback; credential vault provider `youtube` is preferred |
| `YOUTUBE_BASE_URL` | YouTube Data API | Optional YouTube Data API base URL override |
| `SPOTIFY_ACCESS_TOKEN` | Spotify | Optional Spotify bearer token fallback; credential vault provider `spotify` is preferred |
| `SPOTIFY_CLIENT_ID` | Spotify | Optional Spotify client ID for client-credentials catalog tools |
| `SPOTIFY_CLIENT_SECRET` | Spotify | Optional Spotify client secret for client-credentials catalog tools |
| `SPOTIFY_BASE_URL` | Spotify | Optional Spotify Web API base URL override |
| `SPOTIFY_ACCOUNTS_BASE_URL` | Spotify | Optional Spotify Accounts API base URL override |
| `REDDIT_ACCESS_TOKEN` | Reddit | Optional Reddit OAuth bearer token fallback; credential vault provider `reddit` is preferred |
| `REDDIT_REFRESH_TOKEN` | Reddit | Optional Reddit refresh token fallback |
| `REDDIT_CLIENT_ID` | Reddit | Optional Reddit OAuth client ID fallback |
| `REDDIT_CLIENT_SECRET` | Reddit | Optional Reddit OAuth client secret fallback |
| `REDDIT_BASE_URL` | Reddit | Optional Reddit OAuth API base URL override |
| `REDDIT_PUBLIC_BASE_URL` | Reddit | Optional Reddit public JSON API base URL override |
| `REDDIT_TOKEN_URL` | Reddit | Optional Reddit OAuth token URL override |
| `DISCOURSE_API_KEY` | Discourse | Optional Discourse API key fallback; credential vault provider `discourse` is preferred |
| `DISCOURSE_API_USERNAME` | Discourse | Optional Discourse API username fallback |
| `DISCOURSE_BASE_URL` | Discourse | Discourse forum base URL for native tools |
| `MEDIUM_ACCESS_TOKEN` | Medium | Optional Medium access token fallback; credential vault provider `medium` is preferred |
| `MEDIUM_BASE_URL` | Medium | Optional Medium API base URL override |
| `BAMBOOHR_API_KEY` | BambooHR | Optional BambooHR API key fallback; credential vault provider `bamboohr` is preferred |
| `BAMBOOHR_SUBDOMAIN` | BambooHR | Optional BambooHR company subdomain fallback |
| `BAMBOOHR_BASE_URL` | BambooHR | Optional BambooHR gateway base URL override |
| `BEEMINDER_ACCESS_TOKEN` | Beeminder | Optional Beeminder auth token fallback; credential vault provider `beeminder` is preferred |
| `BEEMINDER_BASE_URL` | Beeminder | Optional Beeminder API base URL override |
| `CLOCKIFY_API_KEY` | Clockify | Optional Clockify API key fallback; credential vault provider `clockify` is preferred |
| `CLOCKIFY_BASE_URL` | Clockify | Optional Clockify API base URL override |
| `HARVEST_ACCESS_TOKEN` | Harvest | Optional Harvest access token fallback; credential vault provider `harvest` is preferred |
| `HARVEST_ACCOUNT_ID` | Harvest | Optional Harvest account ID fallback |
| `HARVEST_BASE_URL` | Harvest | Optional Harvest API base URL override |
| `OURA_ACCESS_TOKEN` | Oura | Optional Oura access token fallback; credential vault provider `oura` is preferred |
| `OURA_BASE_URL` | Oura | Optional Oura API base URL override |
| `STRAVA_ACCESS_TOKEN` | Strava | Optional Strava access token fallback; credential vault provider `strava` is preferred |
| `STRAVA_BASE_URL` | Strava | Optional Strava API base URL override |
| `HOMEASSISTANT_ACCESS_TOKEN` | Home Assistant | Optional long-lived token fallback; credential vault provider `homeassistant` is preferred |
| `HOMEASSISTANT_BASE_URL` | Home Assistant | Home Assistant API base URL, usually `http://host:8123/api` |
| `PHILIPS_HUE_ACCESS_TOKEN` | Philips Hue | Optional Philips Hue access token fallback; credential vault provider `philips_hue` is preferred |
| `PHILIPS_HUE_USERNAME` | Philips Hue | Optional Philips Hue bridge username fallback |
| `PHILIPS_HUE_BASE_URL` | Philips Hue | Optional Philips Hue routed API base URL override |
| `ACTIVECAMPAIGN_API_KEY` | ActiveCampaign | Optional ActiveCampaign API key fallback; credential vault provider `activecampaign` is preferred |
| `ACTIVECAMPAIGN_BASE_URL` | ActiveCampaign | ActiveCampaign account API URL, such as `https://account.api-us1.com` |
| `CONVERTKIT_API_SECRET` | ConvertKit | Optional ConvertKit API secret fallback; credential vault provider `convertkit` is preferred |
| `CONVERTKIT_BASE_URL` | ConvertKit | Optional ConvertKit API base URL override |
| `GETRESPONSE_API_KEY` | GetResponse | Optional GetResponse API key fallback; credential vault provider `getresponse` is preferred |
| `GETRESPONSE_BASE_URL` | GetResponse | Optional GetResponse API base URL override |
| `MAILERLITE_API_KEY` | MailerLite | Optional MailerLite API key fallback; credential vault provider `mailerlite` is preferred |
| `MAILERLITE_BASE_URL` | MailerLite | Optional MailerLite API base URL override |
| `MAILERLITE_CLASSIC_API` | MailerLite | Use MailerLite Classic API authentication/header style |
| `ACTIONNETWORK_API_KEY` | Action Network | Optional Action Network API key fallback; credential vault provider `actionnetwork` is preferred |
| `ACTIONNETWORK_BASE_URL` | Action Network | Optional Action Network API base URL override |
| `AUTOPILOT_API_KEY` | Autopilot | Optional Autopilot API key fallback; credential vault provider `autopilot` is preferred |
| `AUTOPILOT_BASE_URL` | Autopilot | Optional Autopilot API base URL override |
| `EGOI_API_KEY` | E-goi | Optional E-goi API key fallback; credential vault provider `egoi` is preferred |
| `EGOI_BASE_URL` | E-goi | Optional E-goi API base URL override |
| `VERO_AUTH_TOKEN` | Vero | Optional Vero auth token fallback; credential vault provider `vero` is preferred |
| `VERO_BASE_URL` | Vero | Optional Vero API base URL override |
| `CUSTOMERIO_TRACKING_SITE_ID` | Customer.io | Optional Customer.io tracking site ID fallback; credential vault provider `customerio` is preferred |
| `CUSTOMERIO_TRACKING_API_KEY` | Customer.io | Optional Customer.io tracking API key fallback |
| `CUSTOMERIO_APP_API_KEY` | Customer.io | Optional Customer.io app API key fallback for campaign reads |
| `CUSTOMERIO_REGION` | Customer.io | Optional Customer.io tracking region host |
| `CUSTOMERIO_TRACKING_BASE_URL` | Customer.io | Optional Customer.io tracking API base URL override |
| `CUSTOMERIO_APP_BASE_URL` | Customer.io | Optional Customer.io app API base URL override |
| `ITERABLE_API_KEY` | Iterable | Optional Iterable API key fallback; credential vault provider `iterable` is preferred |
| `ITERABLE_BASE_URL` | Iterable | Optional Iterable API base URL override |
| `POSTHOG_API_KEY` | PostHog | Optional PostHog project API key fallback; credential vault provider `posthog` is preferred |
| `POSTHOG_BASE_URL` | PostHog | Optional PostHog API base URL override |
| `SEGMENT_WRITE_KEY` | Segment | Optional Segment write key fallback; credential vault provider `segment` is preferred |
| `SEGMENT_BASE_URL` | Segment | Optional Segment tracking API base URL override |
| `COPPER_API_KEY` | Copper | Optional Copper API key fallback; credential vault provider `copper` is preferred |
| `COPPER_EMAIL` | Copper | Copper user email fallback |
| `COPPER_BASE_URL` | Copper | Optional Copper API base URL override |
| `AGILECRM_EMAIL` | Agile CRM | Agile CRM account email fallback |
| `AGILECRM_API_KEY` | Agile CRM | Agile CRM API key fallback; credential vault provider `agilecrm` is preferred |
| `AGILECRM_SUBDOMAIN` | Agile CRM | Agile CRM account subdomain fallback |
| `AGILECRM_BASE_URL` | Agile CRM | Optional Agile CRM API base URL override |
| `MONICA_ACCESS_TOKEN` | Monica CRM | Optional Monica CRM API token fallback; credential vault provider `monica` is preferred |
| `MONICA_BASE_URL` | Monica CRM | Optional Monica CRM API base URL override |

### Additional Settings Model Variables

These variables are also defined by `nymeria.config.settings.Settings` or the
runtime `/settings` environment mapping. They are optional unless a deployment
or tool explicitly requires them.

| Variable | Default | Description |
|----------|---------|-------------|
| `AFFINITY_API_KEY` | - | Affinity API key fallback |
| `AFFINITY_BASE_URL` | `https://api.affinity.co` | Affinity API base URL |
| `ALLOW_UNBOUND_TOOL_CALLS` | `false` | Dynamic binding only. Let the model dispatch a tool by emitting a call for it even when the tool is not in its bound list, provided it passes the same deferred gates as `tool_invoke` (management denylist, role gates, `disabled_tools`). Also drops the resident `tool_invoke` tool from the bound schema to save tokens. Off by default: an unbound call is refused with a redirect to `tool_invoke` (one-off) or `tool_manage` (bind). Only enable on providers that reliably emit calls for tools not present in the schema. |
| `DHL_API_KEY` | - | DHL API key fallback |
| `DHL_BASE_URL` | `https://api-eu.dhl.com` | DHL API base URL |
| `DRIFT_ACCESS_TOKEN` | - | Drift access token fallback |
| `DRIFT_BASE_URL` | `https://driftapi.com` | Drift API base URL |
| `DYNAMIC_TOOL_BINDING` | `true` | Resolve tools per step in the model node instead of rebuilding the graph after tool enablement changes. Set `false` to use the legacy rebuild path as a fallback |
| `EMELIA_API_KEY` | - | Emelia API key fallback |
| `EMELIA_GRAPHQL_URL` | `https://graphql.emelia.io/graphql` | Emelia GraphQL URL |
| `FACEBOOK_ACCESS_TOKEN` | - | Facebook Graph API access token fallback |
| `FACEBOOK_APP_SECRET` | - | Facebook app secret fallback for appsecret_proof |
| `FACEBOOK_GRAPH_BASE_URL` | `https://graph.facebook.com/v23.0` | Facebook Graph API base URL |
| `GEMINI_EXTRACTION_MODEL` | `gemini-3-flash-preview` | Gemini model for attachment text extraction |
| `KEAP_ACCESS_TOKEN` | - | Keap OAuth access token fallback |
| `KEAP_BASE_URL` | `https://api.infusionsoft.com/crm/rest/v1` | Keap REST API base URL |
| `KOBOTOOLBOX_API_TOKEN` | - | KoBoToolbox API token fallback |
| `KOBOTOOLBOX_BASE_URL` | `https://kf.kobotoolbox.org` | KoBoToolbox API root URL |
| `LEMLIST_API_KEY` | - | Lemlist API key fallback |
| `LEMLIST_BASE_URL` | `https://api.lemlist.com/api` | Lemlist API base URL |
| `LINKEDIN_ACCESS_TOKEN` | - | LinkedIn OAuth access token fallback |
| `LINKEDIN_API_VERSION` | `202604` | LinkedIn REST API version header |
| `LINKEDIN_BASE_URL` | `https://api.linkedin.com` | LinkedIn API base URL |
| `MAGENTO_ACCESS_TOKEN` | - | Magento access token fallback |
| `MAGENTO_BASE_URL` | - | Magento REST API base URL override |
| `MAGENTO_HOST` | - | Magento site host fallback |
| `MAUTIC_ACCESS_TOKEN` | - | Mautic OAuth or bearer access token fallback |
| `MAUTIC_BASE_URL` | - | Mautic instance base URL fallback |
| `MAUTIC_PASSWORD` | - | Mautic basic-auth password fallback |
| `MAUTIC_USERNAME` | - | Mautic basic-auth username fallback |
| `MCP_REGISTRY_URL` | `https://registry.modelcontextprotocol.io` | Official MCP registry base URL used by `search_mcp` and `install_mcp_server` |
| `MEMORY_CHAR_LIMIT` | `8000` | Default aggregate character budget for global profile memories and per-thread notepads. Threads can override their notepad limit in thread settings |
| `MEMORY_MAX_ENTRIES` | `100` | Max number of global key-value memories per user (1-10,000). Upserts of existing keys always pass; only new keys are blocked at the cap |
| `MEMORY_VALUE_MAX_CHARS` | `1000` | Max characters stored per global memory value (50-100,000); longer values are truncated |
| `MICROSOFT_GRAPH_ACCESS_TOKEN` | - | Microsoft Graph OAuth access token fallback for native productivity tools |
| `MICROSOFT_GRAPH_BASE_URL` | `https://graph.microsoft.com/v1.0` | Microsoft Graph API base URL |
| `OKTA_ACCESS_TOKEN` | - | Okta SSWS API token fallback |
| `OKTA_BASE_URL` | - | Okta org base URL override |
| `OKTA_DOMAIN` | - | Okta org domain fallback |
| `ONFLEET_API_KEY` | - | Onfleet API key fallback |
| `ONFLEET_BASE_URL` | `https://onfleet.com/api/v2` | Onfleet API base URL |
| `PERPLEXITY_SEARCH_MODEL` | `sonar-pro` | Default Perplexity model for web search |
| `PHANTOMBUSTER_API_KEY` | - | Phantombuster API key fallback |
| `PHANTOMBUSTER_BASE_URL` | `https://api.phantombuster.com/api/v2` | Phantombuster API base URL |
| `QUICKBOOKS_ACCESS_TOKEN` | - | QuickBooks Online OAuth access token fallback |
| `QUICKBOOKS_BASE_URL` | - | QuickBooks Online API base URL override |
| `QUICKBOOKS_ENVIRONMENT` | `production` | QuickBooks environment: `production` or `sandbox` |
| `QUICKBOOKS_REALM_ID` | - | QuickBooks Online company or realm ID fallback |
| `RUNDECK_BASE_URL` | - | Rundeck instance base URL fallback |
| `RUNDECK_TOKEN` | - | Rundeck API token fallback |
| `SENDY_API_KEY` | - | Sendy API key fallback |
| `SENDY_BASE_URL` | - | Sendy base URL fallback |
| `SENDY_URL` | - | Sendy site URL fallback |
| `SMITHERY_API_KEY` | - | Optional Smithery API key for private or verified listings |
| `TELEGRAM_BOT_USERNAME` | - | Public Telegram bot username without `@`, used for `t.me/<bot>?start=...` deep links in the desktop wizard |
| `TWITTER_ACCESS_TOKEN` | - | X/Twitter OAuth access token fallback |
| `TWITTER_API_BASE_URL` | `https://api.twitter.com/2` | X/Twitter API v2 base URL |
| `TWITTER_BEARER_TOKEN` | - | X/Twitter bearer token fallback |
| `UNLEASHED_API_ID` | - | Unleashed API ID fallback |
| `UNLEASHED_API_KEY` | - | Unleashed API key fallback |
| `UNLEASHED_BASE_URL` | `https://api.unleashedsoftware.com` | Unleashed API base URL |
| `WEBFLOW_ACCESS_TOKEN` | - | Webflow access token fallback |
| `WEBFLOW_BASE_URL` | `https://api.webflow.com/v2` | Webflow API base URL |
| `WORKER_MODE` | `false` | Run in worker mode, ticker only without the API server |
| `XERO_ACCESS_TOKEN` | - | Xero OAuth access token fallback |
| `XERO_BASE_URL` | `https://api.xero.com/api.xro/2.0` | Xero Accounting API base URL |
| `XERO_CONNECTIONS_URL` | `https://api.xero.com/connections` | Xero connections API URL |
| `XERO_TENANT_ID` | - | Xero tenant or organization ID fallback |

`nymeria init` collects these optional capability keys during first-run setup.
The interactive flow is a step wizard: step 1 chooses how to host the slim
backend (`local`, `service`, or `docker`), step 2 chooses the provider, API key,
and model, and a review screen confirms before `config.env` is written. It
validates the provider/model/key combination with a small LLM API call (unless
`--skip-llm-test`) before writing config. In non-interactive mode, pass
`--embedding-api-key`, `--openai-api-key`, `--gemini-api-key`, or
`--perplexity-api-key`; keys that are not supplied are left out of `config.env`.
Use `--data-dir` when `NYMERIA_DATA_DIR` should differ from the runtime root's
`data/` directory, and `--root` to relocate both. Non-interactive setup accepts
`--provider`, `--model`, `--api-key` (all required), `--base-url`,
`--api-mode responses|chat_completions`, `--hosting local|service|docker`,
`--port` (writes `API_PORT`; printed URLs, health waits, and the post-start
smoke test follow it),
`--next-action print_commands|cli|start_api_open_frontend`, `--force`, and
`--skip-llm-test`. Add `--run-doctor` (quick) or `--full-doctor` (with a live LLM
check) to validate after writing config. CLIProxy subscription-OAuth provider
routing is deferred and is not part of `nymeria init` in this phase; use a direct
provider API key.

On finish, the wizard prints how to connect: the `nymeria cli` start command
and the backend URL always print, and the freshly minted account token value
prints too when enabled. A near-final toggle controls this (default on in the
interactive wizard); headless runs default to hiding the token so it never
lands in captured stdout, and `--print-creds` / `--no-print-creds` overrides it
in either mode. The token is written to `data/BOOTSTRAP_TOKEN.txt` regardless.

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_BACKEND` | `sqlite` | Backend type: `sqlite`, `postgres`, or `memory` |
| `SQLITE_PATH` | `<data_dir>/nymeria.db` | SQLite database file location. Relative custom paths resolve from the runtime project root |
| `POSTGRES_URI` | - | PostgreSQL connection string (if using postgres) |
| `POSTGRES_POOL_MIN_SIZE` | `1` | Connections the shared LangGraph checkpointer pool keeps open (postgres only) |
| `POSTGRES_POOL_MAX_SIZE` | `10` | Connection ceiling for the shared LangGraph checkpointer pool (postgres only). Sized at process start; a change needs a restart. Keep at or above `CHECKPOINT_EXECUTOR_MAX_WORKERS` so checkpoint threads never queue waiting for a connection |
| `CHECKPOINT_EXECUTOR_MAX_WORKERS` | `8` | Worker-thread ceiling for the dedicated LangGraph checkpoint I/O executor (sqlite and postgres). Checkpoint reads/writes run here instead of the asyncio default executor so persistence never queues behind unrelated blocking work. Sized by the first checkpointer built in the process; a change needs a restart |
| `USER_TIMEZONE` | `UTC` | IANA timezone used for time context and absolute schedule parsing. Docker also mirrors this into `TZ` so OS-level time output stays aligned. |

### API Server and Paths

| Variable | Default | Description |
|----------|---------|-------------|
| `NYMERIA_API_KEY` | - | **Deprecated / ignored.** Formerly a shared bearer token; authentication now uses per-user account tokens. Safe to delete from `.env.docker`. See `docs/accounts.md`. |
| `NYMERIA_SERVICE_TOKEN` | mode-required | Admin-role Nymeria account token used by bots, ticker, trigger-fires, slash commands, and the public MCP thin client for X-Nymeria-Act-As calls. `run.py` fails fast without it for `discord-bot`, `telegram-bot`, `slack-bot`, `mcp`, and `service` (the worker resolves it after its API health wait); local `api`, `cli`, and `users` development can still start without it. Created via `python3 run.py users add --role admin`. See `docs/accounts.md`. |
| `NYMERIA_SECRETS_KEY` | vault-required | 44-character Fernet key used to encrypt credential-vault secret fields, OAuth access/refresh tokens, and BYO bot tokens. Docker deployments must pass it through to the API/worker/MCP containers. If it is missing in the running API container, OAuth/device-code flows can complete at the provider but fail while saving the new credential. |
| `ACCOUNT_TOKEN_TTL_DAYS` | `90` | Lifetime for newly issued Nymeria account tokens. Expired tokens are rejected and auto-revoked. |
| `ACCOUNT_MAX_ACTIVE_TOKENS_PER_USER` | `10` | Maximum non-revoked, non-expired account tokens a user may hold at once. |
| `ACCOUNT_BOOTSTRAP_TOKEN_TTL_HOURS` | `24` | Lifetime for the first-run bootstrap admin token. The plaintext bootstrap token file is also deleted after first successful auth. |
| `NYMERIA_API_URL` | auto | Local API URL for thin clients and in-process tools (MCP server, `slash_command`). Defaults to Docker service URLs when applicable, otherwise `http://localhost:8000` |
| `NYMERIA_PUBLIC_URL` | - | Browser-reachable public API origin used to build one-time credential setup links for chat bots and OAuth authorization-code redirects, e.g. `https://nymeria.example.com`. Desktop modal prompts still work when unset; chat bots will report that a public URL is required. OAuth `request_credential` calls return `status="missing_public_url"` when unset unless the agent retries with `use_localhost=True` (only safe when the user's browser is on the same machine) or the provider supports device-code (currently Outlook). |
| `NYMERIA_ERROR_REPORT_EMAIL` | - | Destination for the desktop "Report problem" button (`POST /report`). The report (thread ID, last 10 messages, client info, optional description) is emailed here via the Outlook email tool, sent from your connected Outlook account. Use a dedicated inbox, not a personal one, since reports carry other users' message content. If unset, `/report` returns 503 and emails no one. |
| `API_HOST` | `0.0.0.0` | Server bind address |
| `API_PORT` | `8000` | Server port. `nymeria init` writes it (the API port wizard step, or `--port` in scripted runs). `run.py slim` resolves an explicit `--port` flag first, then this value, then 8000; the installed background service health-probes the configured port, and the single-container Docker shape publishes it as the host-side port while the container keeps listening on 8000 internally. |
| `NYMERIA_API_DOCS` | `false` | Expose FastAPI Swagger UI, ReDoc, and `/openapi.json`. Disabled by default for beta deployments; changing it requires an API restart |
| `NYMERIA_DEBUG` | `false` | Enables debug-only server behavior, including API docs/schema routes. Use only in trusted local development |
| `CORS_ORIGINS` | `http://localhost:1420,tauri://localhost,http://tauri.localhost,https://tauri.localhost,http://localhost:8000` | Comma-separated allowed CORS origins. Wildcard origins are rejected because credentialed CORS is enabled. The setup wizard's external-access step appends the configured public origin automatically |
| `NYMERIA_EXTERNAL_ACCESS` | - | Setup-wizard round-trip marker for the external-access choice (`local_only`, `tailscale`, `cloudflare`, `chat_bots`). The runtime does not read it; `nymeria init` writes it so a reconfigure can restore the choice, alongside the real settings (`NYMERIA_PUBLIC_URL`, `CORS_ORIGINS`) |
| `NYMERIA_HOSTING` | - | Setup-wizard round-trip marker for the hosting choice (`local`, `service`). The runtime does not read it; `nymeria init` writes it so a reconfigure restores LOCAL vs SERVICE even while an old background-service unit is still installed |
| `NYMERIA_FORWARDED_ALLOW_IPS` | - | Comma-separated trusted proxy IPs. When set, uvicorn runs with `--proxy-headers` and honors `X-Forwarded-For` only from these IPs, so the per-IP auth-failure limiter keys on the real client (set to your reverse proxy's IP, e.g. `127.0.0.1` for a colocated Caddy). Unset means the header is not trusted. |
| `NYMERIA_USER_REQUEST_RATE_LIMIT` | `300` | Per-user request cap per 60s on the expensive endpoints (`/chat`, `/chat/sync`, `/voice/*`, `/commands/execute`) to bound runaway LLM/STT spend from a compromised token. Admin-role callers (including the worker's autonomous-turn relay) are exempt. Set `0` to disable. |
| `NYMERIA_MCP_ALLOW_UNAUTHENTICATED` | `false` | Escape hatch that disables inbound bearer auth on the MCP streamable-HTTP server. Leave off; only enable for a fully trusted, loopback-only local setup. |
| `NYMERIA_DATA_DIR` | `<project_root>/data` | Override data directory path. For pipx/wheel installs, the project root defaults to `~/.nymeria`, so the effective default is `~/.nymeria/data` |
| `NYMERIA_SNAPSHOTS_DIR` | `<data_dir>/snapshots` | Override where `snapshot create` writes user-data backup artifacts. See `docs/deployment/backup-and-restore.md` |
| `NYMERIA_SNAPSHOT_PASSPHRASE` | - | Passphrase for `snapshot` create/verify/restore when running non-interactively (alternative to `--passphrase-file`). Never persisted by Nymeria; set it in the invoking environment only |
| `NYMERIA_WORKSPACE_DIR` | `/workspace` | Workspace root for generated artifacts and optional file-tool confinement |
| `NYMERIA_PROJECT_ROOT` | auto-detected | Override runtime project root resolution. Source launches use the checkout's `Nymeria/` root; packaged/frozen launches default to `~/.nymeria` |
| `NYMERIA_CONFINE_FILE_TO_WORKSPACE` | `false` | When true, `file_write` and `file_edit` reject write targets outside `NYMERIA_WORKSPACE_DIR`. False keeps broad personal-assistant file access and relies on deployment sandboxing |
| `NYMERIA_ALLOW_SELF_EDIT` | `true` | Enables admin-only `self_file_write`, `self_file_delete`, and `self_reload`; set false to disable self-modifying maintenance tools |
| `NYMERIA_ALLOW_UNSANDBOXED_MCP_INSTALL` | `true` | Enables managed MCP installs that execute downloaded package/bundle code inside the current backend environment. Set false to require an external sandbox/maintenance workflow |
| `NYMERIA_ENFORCE_MCP_STDIO_ALLOWLIST` | `true` | Restricts MCP stdio launches to the `SAFE_STDIO_COMMANDS` allowlist (`uvx`/`uv`/`npx`/`npm`/`node`/`python`/`deno`/`bun`) plus `NYMERIA_MCP_EXTRA_STDIO_COMMANDS`, and rejects unsafe eval flags. Set false to allow any launcher |
| `NYMERIA_MCP_EXTRA_STDIO_COMMANDS` | `` (empty) | Comma-separated extra launcher basenames added to the MCP stdio allowlist (e.g. `docker,podman`) |

Project-root auto-detection first honors `NYMERIA_PROJECT_ROOT`. Source
launches walk upward looking for Nymeria backend markers such as `run.py`,
`docker-compose.yml`, and `nymeria/config/soul.md`; the older fixed-depth path
from `nymeria/config/settings.py` remains only as a compatibility fallback.
Wheel/pipx installs and manual frozen backend builds without an explicit
override use the packaged runtime root, `~/.nymeria/`, for config and writable
data. Set `NYMERIA_PROJECT_ROOT` explicitly if a packaged backend deployment
needs checkout-style paths or separates the Python package from the runtime
config directory.

The `nymeria` console script bootstraps this automatically. When it runs from a
source checkout or editable install, it uses the checkout's `Nymeria/` backend
root. When it runs from a wheel/pipx install, it uses `~/.nymeria/` for
`config.env`, `data/`, and logs while loading bundled package assets such as
`nymeria/config/soul.md` from the installed Python package. `nymeria init`
writes an explicit `NYMERIA_DATA_DIR=<root>/data` line to `config.env` by
default, or the custom `--data-dir` value, so a later root or data-directory
move should update that value or rerun `nymeria init --root ...` or
`nymeria init --data-dir ...`.
The beta Windows desktop release is client-only: it does not bundle a backend
executable, does not read or write backend config files, and does not set
`NYMERIA_PROJECT_ROOT` for a local backend process.

`nymeria init` offers to run a final doctor check after writing config. When
the setup wizard already validated provider auth, that final check defaults to
`nymeria doctor --skip-llm-test` to avoid a duplicate live LLM call; choose the
full doctor option only when you want the second provider check. Run
`nymeria doctor` after manual config edits to check the effective Python
version, config files, data directory, LLM connectivity, SQLite/Postgres state,
optional Redis/voice setup, bundled frontend, and API port. Use
`nymeria doctor --skip-llm-test` when diagnosing an offline system or when
provider credentials are intentionally unavailable.

Runtime settings updates choose the highest-precedence existing config file:
`.env.docker`, then `config.env`, then `.env`. If no config file exists yet,
source checkouts create `.env`; packaged installs create `config.env`.
Admins can test a provider/model/key/base-URL combination before writing it with
`POST /settings/llm/test`; the probe accepts direct providers and CLIProxy-shaped
base URLs, but does not persist the submitted API key and does not echo secrets
in the response. Admins can then persist deployment-wide provider and capability
keys with `PATCH /settings` using explicit write-only fields such as
`openai_api_key`, `anthropic_api_key`, `anthropic_direct_api_key`,
`openrouter_api_key`, `embedding_api_key`, `gemini_api_key`, and
`perplexity_api_key`. These keys remain absent from `GET /settings`; the admin
environment listing masks secret values.

The `/provider` slash command (central command registry, so it works from the
CLI, desktop, and bots) exposes the LLM provider flow. `/provider set
<provider> api_key=<key>` applies the mapped write-only backend settings
(admin only; no client-side copy is kept since the 2026-07 config-group
migration retired the CLI-local `~/.nymeria/credentials.json` store).
`/provider test [provider]` calls the same probe as `POST /settings/llm/test`,
resolving the credential server-side from the vault, settings, and environment
in that order. `/provider switch <provider>` patches `llm_provider` and warns
when no server credential exists for the target provider.

The desktop app exposes this flow in Settings > Provider > Open Wizard for admin
accounts. The wizard can save direct provider keys or configure the backend to
use an already-running CLIProxy OAuth endpoint. It does not start or manage
CLIProxy in installed client-only desktop builds; the proxy URL must be
reachable from the backend process.

### Browser Tools

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_FORCE_FALLBACK` | `false` | Skip Playwright and use requests+BeautifulSoup fallback mode for browser navigation/content extraction |
| `BROWSER_HEADLESS` | auto | Force Playwright headless mode with `true` or visible mode with `false`; unset auto-detects Docker/headless Linux |
| `BROWSER_VERIFY_SSL` | ignored | Deprecated. TLS verification is always enforced for browser fallback requests |

### Redis (Docker Only)

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_ENABLED` | `false` | Enable Redis event bus for cross-container communication |
| `REDIS_PASSWORD` | required in Docker | Redis password used by the Compose Redis service and the generated `REDIS_URL`. Use a URL-safe value such as `openssl rand -hex 32`. |
| `REDIS_URL` | - | Redis connection URL. Docker Compose generates `redis://:<REDIS_PASSWORD>@redis:6379/0`; local non-Docker development can use `redis://localhost:6379` if Redis auth is disabled. Startup logs redact credentials from this URL. |

### Container Resource Limits (Docker Only)

The Compose stack caps each container's memory, CPU, and PID count as a safety
boundary (blast-radius containment for a compromised tool call) and, for the
API, to survive memory spikes from tool subprocesses. The API memory limits are
env-overridable; the rest are literals in `docker-compose.yml`. The caps are NOT
auto-sized from host RAM yet (a cgroup-aware `nymeria init` sizing step is a
planned follow-up), so tune them to your host.

| Variable | Default | Description |
|----------|---------|-------------|
| `NYMERIA_API_MEM_LIMIT` | `2g` | API container memory cap. The API runs the agent (the largest process); raise on bigger hosts (e.g. `2560m` on 8 GB, `4g` on 16 GB). |
| `NYMERIA_API_MEMSWAP_LIMIT` | `3g` | API container memory+swap total. Must be `>= NYMERIA_API_MEM_LIMIT`; the excess is swap headroom so a spike slows (swaps) instead of an instant OOM kill. Only effective if the host has swap; set equal to `NYMERIA_API_MEM_LIMIT` for no swap. |

**Minimum host:** the full Compose stack (API + Postgres + Redis + agent) is
sized for a host with roughly 4 GB+ RAM. On smaller hosts, lower
`NYMERIA_API_MEM_LIMIT` (or use the single-container slim shape instead).

**Why the API is protected:** under memory pressure the kernel OOM-killer evicts
the largest process, which is the API itself. Tool subprocesses (bash,
`claude_code`, custom python tools, MCP servers) are therefore spawned with a
raised `oom_score_adj` so the kernel evicts the offending tool first and the API
survives the turn. That protection is hardware-agnostic and independent of these
caps. See `nymeria/oom.py`.

### Claude Code Bridge (`claude_code` tool)

The admin-only `claude_code` tool drives Claude Code where the repo and real auth
live. In Docker it relays runs to a host runner; with `NYMERIA_CLAUDE_CODE_URL`
unset it runs Claude Code locally in-process. Full runbook:
`docs/agent-systems/claude-code-bridge.md`.

| Variable | Default | Description |
|----------|---------|-------------|
| `NYMERIA_CLAUDE_CODE_URL` | - | Host runner base URL (e.g. `http://host.docker.internal:8200`). Unset = local in-process mode. |
| `NYMERIA_CLAUDE_CODE_TOKEN` | - | Bearer token shared by the tool and the runner. Always set it for a networked runner. |
| `NYMERIA_CLAUDE_CODE_ROOTS` | project root | Allowed working-directory roots (os.pathsep or comma separated). The directory sandbox. |
| `NYMERIA_CLAUDE_CODE_MODEL` | - | Model alias/id Claude Code runs with (e.g. `opus`). Empty = Claude Code's own default. |
| `NYMERIA_CLAUDE_CODE_FALLBACK_MODEL` | - | Fallback model when the primary is unavailable. |
| `NYMERIA_CLAUDE_CODE_MAX_TURNS` | - | Cap on Claude Code ReAct turns per run. Empty = no explicit cap. |
| `NYMERIA_CLAUDE_CODE_MAX_BUDGET_USD` | - | Per-run USD budget cap. Empty = none (subscription/OAuth auth bills $0). |
| `NYMERIA_CLAUDE_CODE_MAX_CONCURRENCY` | `2` | Max concurrent runs the host runner executes at once. Bounds host memory under bursts. |
| `NYMERIA_CLAUDE_CODE_ALLOWED_MODELS` | - | Allowlist of models a per-thread override may request on the runner (comma/os.pathsep). Empty = accept any (budget caps still apply). |
| `NYMERIA_CLAUDE_CODE_DISALLOWED_TOOLS` | built-in | Override the hard deny list (comma/os.pathsep). Empty = defaults (`rm`, `git push`, `sudo`, ...). |
| `NYMERIA_CLAUDE_CODE_DEFAULT_MODE` | `dontAsk` | Default permission mode when the agent passes none (`default`/`plan`/`acceptEdits`/`dontAsk`/`auto`/`bypass`). |
| `NYMERIA_CLAUDE_CODE_BARE` | `false` | Run with `--bare` (skips hooks/CLAUDE.md; forces `ANTHROPIC_API_KEY` auth instead of OAuth/keychain). |
| `NYMERIA_CLAUDE_CODE_BLOCK_SECONDS` | - | Max seconds the tool blocks inline before detaching a long run. Empty = derive from `tool_timeout`. |

### Messaging Platforms

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | - | Telegram bot token from @BotFather |
| `TELEGRAM_API_BASE_URL` | `https://api.telegram.org` | Telegram Bot API base URL for native Telegram tools |
| `TELEGRAM_DEFAULT_CHAT_ID` | - | Default Telegram chat ID for notifications |
| `WHATSAPP_ACCESS_TOKEN` | - | WhatsApp Business Cloud access token fallback |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | - | WhatsApp business account ID fallback |
| `WHATSAPP_PHONE_NUMBER_ID` | - | WhatsApp sender phone number ID fallback |
| `WHATSAPP_BASE_URL` | `https://graph.facebook.com/v19.0` | WhatsApp Graph API base URL |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | - | WhatsApp Cloud webhook challenge token |
| `WHATSAPP_APP_SECRET` | - | Required Meta app secret for webhook signature verification |
| `WHATSAPP_SHOW_TOOL_EVENTS` | `false` | Show compact tool-call events in WhatsApp bot replies |
| `DISCORD_WEBHOOK_URL` | - | Discord webhook URL for notifications |
| `DISCORD_BOT_TOKEN` | - | Discord bot token for two-way communication |
| `DISCORD_MODE` | `gateway` | Discord connection mode: `gateway` or `webhook` |
| `DISCORD_RESPOND_MODE` | `mention` | Guild behavior: `mention` (only @Nymeria) or `all` |
| `SLACK_WEBHOOK_URL` | - | Slack webhook URL for notifications |
| `SLACK_BOT_TOKEN` | - | Slack bot token for two-way communication |
| `SLACK_APP_TOKEN` | - | Slack app-level token for Socket Mode (`xapp-...`, requires `connections:write`) |
| `SLACK_RESPOND_MODE` | `mention` | Slack channel behavior: `mention` (DMs and @mentions) or `all` |
| `SLACK_SHOW_TOOL_EVENTS` | `false` | Show compact Slack tool call/result messages during streamed replies |
| `TEAMS_BOT_APP_ID` | - | Bot Framework app ID for Teams webhook replies |
| `TEAMS_BOT_APP_PASSWORD` | - | Bot Framework client secret for Teams webhook replies |
| `TEAMS_BOT_TENANT_ID` | - | Optional Azure tenant ID used during setup |
| `TEAMS_BOT_RESPOND_MODE` | `mention` | Teams group/channel behavior: `mention` or `all` |
| `TEAMS_BOT_VALIDATE_AUTH` | ignored | Deprecated. Bot Framework bearer-token validation is always enforced |
| `TEAMS_BOT_SHOW_TOOL_EVENTS` | `false` | Show compact Teams tool call/result messages during streamed replies |
| `TEAMS_BOT_TOKEN_URL` | `https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token` | Bot Framework OAuth token URL |
| `TEAMS_BOT_OPENID_CONFIG_URL` | `https://login.botframework.com/v1/.well-known/openidconfiguration` | Bot Framework OpenID metadata URL |
| `TEAMS_TEAM_ID` | - | Microsoft Teams team ID for notifications |
| `TEAMS_CHANNEL_ID` | - | Microsoft Teams channel ID for notifications |
| `TEAMS_ACCOUNT_ID` | - | Outlook account ID for Teams (must have ChannelMessage.Send) |
| `OUTLOOK_DEFAULT_ACCOUNT_ID` | - | Default Outlook account for email tools |
| `MICROSOFT_MCP_CLIENT_ID` | - | Azure AD app client ID for Outlook/Teams OAuth |
| `GOOGLE_OAUTH_CREDENTIALS` | - | Path to Google OAuth installed-app credentials JSON file. Used by both the legacy `*_auth_start` tools and the new unified `request_credential(kind="oauth")` flow for Google providers. Required for any Google OAuth path; user tokens are stored in the vault as `kind=oauth_token`, but the client secret itself stays in this file (one per Nymeria install). |
| `PERPLEXITY_API_KEY` | - | Perplexity API key for web_search_perplexity tool |
| `TAVILY_API_KEY` | - | Tavily API key for web_search_tavily tool |
| `EXA_API_KEY` | - | Exa API key for web_search_exa_ai tool |
| `FIRECRAWL_API_KEY` | - | Firecrawl API key for web_search_firecrawl tool |
| `BRAVE_API_KEY` | - | Brave Search API key for web_search_brave tool |
| `WOLFRAM_ALPHA_APP_ID` | - | Wolfram\|Alpha AppID for wolfram_alpha_query |
| `SEARXNG_BASE_URL` | - | Base URL for a SearXNG instance used by web_search_searxng (Docker compose sets `http://searxng:8080` for the bundled sidecar; `nymeria init` writes it when SearXNG is selected on a Docker host) |
| `SEARXNG_SECRET` | (compose default) | Cookie/CSRF signing secret the compose files interpolate into the SearXNG sidecar; `nymeria init` generates one per install when the sidecar is selected |
| `NASA_API_KEY` | - | NASA API key fallback for nasa_apod |
| `OPENWEATHERMAP_API_KEY` | - | OpenWeatherMap API key fallback for weather tools |
| `NPM_REGISTRY_URL` | `https://registry.npmjs.org` | npm registry base URL fallback |
| `CRYPTO_HMAC_SECRET` | - | Crypto HMAC secret fallback |
| `CRYPTO_SIGN_PRIVATE_KEY` | - | Crypto signing private key PEM fallback |
| `CRYPTO_SIGN_PRIVATE_KEY_PASSPHRASE` | - | Crypto signing private key passphrase fallback |
| `JWT_SECRET` | - | JWT HMAC secret fallback |
| `JWT_PRIVATE_KEY` | - | JWT signing private key PEM fallback |
| `JWT_PUBLIC_KEY` | - | JWT verification public key PEM fallback |
| `JWT_ALGORITHM` | `HS256` | Default JWT signing/verification algorithm |
| `GITHUB_TOKEN` | - | GitHub API token fallback for developer-platform tools |
| `GITHUB_API_BASE_URL` | `https://api.github.com` | GitHub API base URL |
| `GITLAB_TOKEN` | - | GitLab API token fallback for developer-platform tools |
| `GITLAB_BASE_URL` | `https://gitlab.com/api/v4` | GitLab API base URL or instance URL |
| `CIRCLECI_API_TOKEN` | - | CircleCI API token fallback |
| `CIRCLECI_BASE_URL` | `https://circleci.com/api/v2` | CircleCI API base URL |
| `TRAVISCI_API_TOKEN` | - | Travis CI API token fallback |
| `TRAVISCI_BASE_URL` | `https://api.travis-ci.com` | Travis CI API base URL |
| `JENKINS_BASE_URL` | - | Jenkins instance URL fallback |
| `JENKINS_USERNAME` | - | Jenkins username fallback |
| `JENKINS_API_TOKEN` | - | Jenkins API token fallback |
| `DROPBOX_ACCESS_TOKEN` | - | Dropbox access token fallback |
| `DROPBOX_API_BASE_URL` | `https://api.dropboxapi.com/2` | Dropbox API base URL |
| `DROPBOX_CONTENT_BASE_URL` | `https://content.dropboxapi.com/2` | Dropbox content API base URL |
| `NEXTCLOUD_WEBDAV_URL` | - | Nextcloud WebDAV URL fallback |
| `NEXTCLOUD_USERNAME` | - | Nextcloud username fallback |
| `NEXTCLOUD_PASSWORD` | - | Nextcloud password or app password fallback |
| `NEXTCLOUD_ACCESS_TOKEN` | - | Nextcloud OAuth access token fallback |
| `AWS_ACCESS_KEY_ID` | - | AWS access key ID fallback for S3 and AWS service tools |
| `AWS_SECRET_ACCESS_KEY` | - | AWS secret access key fallback for S3 and AWS service tools |
| `AWS_SESSION_TOKEN` | - | AWS session token fallback for S3 and AWS service tools |
| `AWS_REGION` | `us-east-1` | AWS region fallback for S3 and AWS service tools |
| `AWS_ENDPOINT_URL_S3` | - | S3-compatible endpoint URL fallback |
| `S3_FORCE_PATH_STYLE` | `false` | Use path-style S3 addressing |
| `CLEARBIT_API_KEY` | - | Clearbit API key fallback |
| `CLEARBIT_COMPANY_BASE_URL` | `https://company-stream.clearbit.com` | Clearbit company API base URL |
| `CLEARBIT_PERSON_BASE_URL` | `https://person-stream.clearbit.com` | Clearbit person API base URL |
| `CLEARBIT_AUTOCOMPLETE_BASE_URL` | `https://autocomplete.clearbit.com` | Clearbit autocomplete API base URL |
| `UPLEAD_API_KEY` | - | Uplead API key fallback |
| `UPLEAD_BASE_URL` | `https://api.uplead.com/v2` | Uplead API base URL |
| `DROPCONTACT_API_KEY` | - | Dropcontact API key fallback |
| `DROPCONTACT_BASE_URL` | `https://api.dropcontact.io` | Dropcontact API base URL |
| `HUMANTIC_API_KEY` | - | Humantic AI API key fallback |
| `HUMANTIC_BASE_URL` | `https://api.humantic.ai/v1` | Humantic AI API base URL |
| `LONESCALE_API_KEY` | - | LoneScale API key fallback |
| `LONESCALE_BASE_URL` | `https://public-api.lonescale.com` | LoneScale API base URL |
| `UPROC_EMAIL` | - | uProc account email fallback |
| `UPROC_API_KEY` | - | uProc API key fallback |
| `UPROC_BASE_URL` | `https://api.uproc.io/api/v2` | uProc API base URL |
| `BITLY_TOKEN` | - | Bitly API token fallback |
| `BITLY_BASE_URL` | `https://api-ssl.bitly.com/v4` | Bitly API base URL |
| `BRANDFETCH_API_KEY` | - | Brandfetch API key fallback |
| `BRANDFETCH_BASE_URL` | `https://api.brandfetch.io/v2` | Brandfetch API base URL |
| `MARKETSTACK_API_KEY` | - | Marketstack API key fallback |
| `MARKETSTACK_BASE_URL` | `https://api.marketstack.com/v1` | Marketstack API base URL |
| `DEEPL_API_KEY` | - | DeepL API key fallback |
| `DEEPL_API_PLAN` | `pro` | DeepL API plan, `pro` or `free` |
| `DEEPL_BASE_URL` | - | DeepL API base URL override |
| `LINGVANEX_API_KEY` | - | LingvaNex API key fallback |
| `LINGVANEX_BASE_URL` | `https://api-b2b.backenster.com/b1/api/v3` | LingvaNex API base URL |
| `APITEMPLATE_API_KEY` | - | APITemplate API key fallback |
| `APITEMPLATE_BASE_URL` | `https://api.apitemplate.io/v1` | APITemplate API base URL |
| `ONESIMPLE_API_TOKEN` | - | One Simple API token fallback |
| `ONESIMPLE_BASE_URL` | `https://onesimpleapi.com/api` | One Simple API base URL |
| `ERPNEXT_API_KEY` | - | ERPNext API key fallback |
| `ERPNEXT_API_SECRET` | - | ERPNext API secret fallback |
| `ERPNEXT_BASE_URL` | - | ERPNext site base URL |
| `ERPNEXT_SUBDOMAIN` | - | ERPNext cloud site subdomain fallback |
| `ERPNEXT_CLOUD_DOMAIN` | `erpnext.com` | ERPNext cloud domain |
| `ODOO_URL` | - | Odoo site URL fallback |
| `ODOO_USERNAME` | - | Odoo username fallback |
| `ODOO_PASSWORD` | - | Odoo password or API key fallback |
| `ODOO_DATABASE` | - | Odoo database name fallback |
| `INVOICENINJA_API_TOKEN` | - | Invoice Ninja API token fallback |
| `INVOICENINJA_SECRET` | - | Invoice Ninja v5 API secret fallback |
| `INVOICENINJA_BASE_URL` | `https://invoicing.co` | Invoice Ninja API base URL |
| `INVOICENINJA_API_VERSION` | `v5` | Invoice Ninja API version |
| `DEMIO_API_KEY` | - | Demio API key fallback |
| `DEMIO_API_SECRET` | - | Demio API secret fallback |
| `DEMIO_BASE_URL` | `https://my.demio.com/api/v1` | Demio API base URL |
| `ZOOM_ACCESS_TOKEN` | - | Zoom OAuth or bearer access token fallback |
| `ZOOM_BASE_URL` | `https://api.zoom.us/v2` | Zoom API base URL |
| `GOTOWEBINAR_ACCESS_TOKEN` | - | GoToWebinar OAuth access token fallback |
| `GOTOWEBINAR_ACCOUNT_KEY` | - | GoToWebinar account key fallback |
| `GOTOWEBINAR_ORGANIZER_KEY` | - | GoToWebinar organizer key fallback |
| `GOTOWEBINAR_BASE_URL` | `https://api.getgo.com/G2W/rest/v2` | GoToWebinar API base URL |
| `TODOIST_API_KEY` | - | Todoist API key fallback |
| `TODOIST_BASE_URL` | `https://api.todoist.com/api/v1` | Todoist API base URL |
| `TRELLO_API_KEY` | - | Trello API key fallback |
| `TRELLO_API_TOKEN` | - | Trello API token fallback |
| `TRELLO_BASE_URL` | `https://api.trello.com/1` | Trello API base URL |
| `RAINDROP_ACCESS_TOKEN` | - | Raindrop access token fallback |
| `RAINDROP_BASE_URL` | `https://api.raindrop.io/rest/v1` | Raindrop API base URL |
| `YOURLS_URL` | - | YOURLS site or API URL fallback |
| `YOURLS_SIGNATURE` | - | YOURLS signature token fallback |
| `YOURLS_USERNAME` | - | YOURLS username fallback |
| `YOURLS_PASSWORD` | - | YOURLS password fallback |
| `ASANA_ACCESS_TOKEN` | - | Asana personal access token fallback |
| `ASANA_BASE_URL` | `https://app.asana.com/api/1.0` | Asana API base URL |
| `LINEAR_API_KEY` | - | Linear API key fallback |
| `LINEAR_API_URL` | `https://api.linear.app/graphql` | Linear GraphQL API URL |
| `GRAPHQL_ENDPOINT` | - | Generic GraphQL endpoint fallback |
| `GRAPHQL_BEARER_TOKEN` | - | Generic GraphQL bearer token fallback |
| `GRAPHQL_API_KEY` | - | Generic GraphQL API key fallback |
| `GRAPHQL_API_KEY_HEADER` | `x-api-key` | Generic GraphQL API key header name |
| `GRAPHQL_HEADERS_JSON` | `{}` | Generic GraphQL default headers as a JSON object |
| `TOTP_SECRET` | - | Base32 TOTP secret fallback |
| `JIRA_EMAIL` | - | Jira Cloud account email fallback |
| `JIRA_API_TOKEN` | - | Jira Cloud API token fallback |
| `JIRA_ACCESS_TOKEN` | - | Jira OAuth/bearer token fallback |
| `JIRA_BASE_URL` | - | Jira Cloud site base URL |
| `CLICKUP_ACCESS_TOKEN` | - | ClickUp access token fallback |
| `CLICKUP_BASE_URL` | `https://api.clickup.com/api/v2` | ClickUp API base URL |
| `MONDAY_API_TOKEN` | - | Monday API token fallback |
| `MONDAY_API_URL` | `https://api.monday.com/v2` | Monday GraphQL API URL |
| `TAIGA_AUTH_TOKEN` | - | Taiga auth token fallback |
| `TAIGA_USERNAME` | - | Taiga username fallback |
| `TAIGA_PASSWORD` | - | Taiga password fallback |
| `TAIGA_BASE_URL` | `https://api.taiga.io/api/v1` | Taiga API base URL |
| `WEKAN_BASE_URL` | - | Wekan instance root URL |
| `WEKAN_TOKEN` | - | Wekan session token fallback |
| `WEKAN_USERNAME` | - | Wekan username fallback |
| `WEKAN_PASSWORD` | - | Wekan password fallback |
| `SLACK_BOT_TOKEN` | - | Slack bot token fallback for native Slack tools and the Slack bot |
| `SLACK_ACCESS_TOKEN` | - | Slack access token fallback |
| `SLACK_BASE_URL` | `https://slack.com/api` | Slack Web API base URL |
| `NOTION_API_KEY` | - | Notion API key fallback |
| `NOTION_VERSION` | `2026-03-11` | Notion API version |
| `NOTION_BASE_URL` | `https://api.notion.com/v1` | Notion API base URL |
| `AIRTABLE_ACCESS_TOKEN` | - | Airtable personal access token fallback |
| `AIRTABLE_API_KEY` | - | Airtable legacy API key fallback |
| `AIRTABLE_BASE_URL` | `https://api.airtable.com/v0` | Airtable API base URL |
| `HUBSPOT_ACCESS_TOKEN` | - | HubSpot private app/OAuth token fallback |
| `HUBSPOT_BASE_URL` | `https://api.hubapi.com` | HubSpot API base URL |
| `ZENDESK_EMAIL` | - | Zendesk email fallback for API-token auth |
| `ZENDESK_API_TOKEN` | - | Zendesk API token fallback |
| `ZENDESK_ACCESS_TOKEN` | - | Zendesk OAuth access token fallback |
| `ZENDESK_SUBDOMAIN` | - | Zendesk subdomain fallback |
| `ZENDESK_BASE_URL` | - | Zendesk API base URL override |
| `MAILCHIMP_API_KEY` | - | Mailchimp API key fallback |
| `MAILCHIMP_ACCESS_TOKEN` | - | Mailchimp OAuth access token fallback |
| `MAILCHIMP_SERVER_PREFIX` | - | Mailchimp server prefix, e.g. `us21` |
| `MAILCHIMP_BASE_URL` | - | Mailchimp Marketing API base URL override |
| `FRESHDESK_API_KEY` | - | Freshdesk API key fallback |
| `FRESHDESK_DOMAIN` | - | Freshdesk account subdomain fallback |
| `FRESHDESK_BASE_URL` | - | Freshdesk API base URL override |
| `FRESHSERVICE_API_KEY` | - | Freshservice API key fallback |
| `FRESHSERVICE_DOMAIN` | - | Freshservice account subdomain fallback |
| `FRESHSERVICE_BASE_URL` | - | Freshservice API base URL override |
| `SERVICENOW_BASE_URL` | - | ServiceNow API base URL override |
| `SERVICENOW_INSTANCE` | - | ServiceNow instance subdomain fallback |
| `SERVICENOW_ACCESS_TOKEN` | - | ServiceNow OAuth/bearer token fallback |
| `SERVICENOW_USERNAME` | - | ServiceNow basic-auth username fallback |
| `SERVICENOW_PASSWORD` | - | ServiceNow basic-auth password fallback |
| `ZAMMAD_BASE_URL` | - | Zammad API base URL fallback |
| `ZAMMAD_TOKEN` | - | Zammad token fallback |
| `ZAMMAD_USERNAME` | - | Zammad basic-auth username fallback |
| `ZAMMAD_PASSWORD` | - | Zammad basic-auth password fallback |
| `HELPSCOUT_ACCESS_TOKEN` | - | Help Scout OAuth access token fallback |
| `HELPSCOUT_BASE_URL` | `https://api.helpscout.net/v2` | Help Scout API base URL |
| `INTERCOM_ACCESS_TOKEN` | - | Intercom access token fallback |
| `INTERCOM_BASE_URL` | `https://api.intercom.io` | Intercom API base URL |
| `INTERCOM_VERSION` | `2.11` | Intercom API version header |
| `SALESFORCE_INSTANCE_URL` | - | Salesforce instance URL fallback |
| `SALESFORCE_ACCESS_TOKEN` | - | Salesforce OAuth access token fallback |
| `SALESFORCE_BASE_URL` | - | Salesforce API base URL override |
| `SALESFORCE_API_VERSION` | `v59.0` | Salesforce REST API version |
| `ZOHO_CRM_ACCESS_TOKEN` | - | Zoho CRM OAuth access token fallback |
| `ZOHO_CRM_API_DOMAIN` | - | Zoho CRM API domain fallback |
| `ZOHO_CRM_BASE_URL` | `https://www.zohoapis.com/crm/v2` | Zoho CRM API base URL |
| `FRESHWORKS_CRM_API_KEY` | - | Freshworks CRM API key fallback |
| `FRESHWORKS_CRM_DOMAIN` | - | Freshworks CRM account domain fallback |
| `FRESHWORKS_CRM_BASE_URL` | - | Freshworks CRM API base URL override |
| `SALESMATE_SESSION_TOKEN` | - | Salesmate session token fallback |
| `SALESMATE_LINK_NAME` | - | Salesmate link name fallback |
| `SALESMATE_BASE_URL` | `https://apis.salesmate.io` | Salesmate API base URL |
| `PIPEDRIVE_API_TOKEN` | - | Pipedrive API token fallback |
| `PIPEDRIVE_ACCESS_TOKEN` | - | Pipedrive OAuth access token fallback |
| `PIPEDRIVE_BASE_URL` | `https://api.pipedrive.com/api/v2` | Pipedrive API base URL |
| `TWILIO_ACCOUNT_SID` | - | Twilio account SID fallback |
| `TWILIO_AUTH_TOKEN` | - | Twilio auth token or API key secret fallback |
| `TWILIO_API_KEY_SID` | - | Optional Twilio API key SID fallback |
| `TWILIO_BASE_URL` | `https://api.twilio.com/2010-04-01` | Twilio API base URL |
| `SENDGRID_API_KEY` | - | SendGrid API key fallback |
| `SENDGRID_BASE_URL` | `https://api.sendgrid.com/v3` | SendGrid API base URL |
| `MAILGUN_API_KEY` | - | Mailgun API key fallback |
| `MAILGUN_DOMAIN` | - | Mailgun sending domain fallback |
| `MAILGUN_BASE_URL` | `https://api.mailgun.net/v3` | Mailgun API base URL |
| `BREVO_API_KEY` | - | Brevo API key fallback |
| `BREVO_BASE_URL` | `https://api.brevo.com/v3` | Brevo API base URL |
| `MAILJET_API_KEY` | - | Mailjet email API key fallback |
| `MAILJET_SECRET_KEY` | - | Mailjet email secret key fallback |
| `MAILJET_SMS_TOKEN` | - | Mailjet SMS token fallback |
| `MAILJET_BASE_URL` | `https://api.mailjet.com` | Mailjet API base URL |
| `MANDRILL_API_KEY` | - | Mandrill / Mailchimp Transactional API key fallback |
| `MANDRILL_BASE_URL` | `https://mandrillapp.com/api/1.0` | Mandrill API base URL |
| `MESSAGEBIRD_ACCESS_KEY` | - | MessageBird access key fallback |
| `MESSAGEBIRD_BASE_URL` | `https://rest.messagebird.com` | MessageBird API base URL |
| `MOCEAN_API_KEY` | - | Mocean API key fallback |
| `MOCEAN_API_SECRET` | - | Mocean API secret fallback |
| `MOCEAN_BASE_URL` | `https://rest.moceanapi.com` | Mocean API base URL |
| `MSG91_AUTH_KEY` | - | MSG91 authentication key fallback |
| `MSG91_BASE_URL` | `https://api.msg91.com/api` | MSG91 API base URL |
| `PLIVO_AUTH_ID` | - | Plivo auth ID fallback |
| `PLIVO_AUTH_TOKEN` | - | Plivo auth token fallback |
| `PLIVO_BASE_URL` | `https://api.plivo.com/v1` | Plivo API base URL |
| `VONAGE_API_KEY` | - | Vonage API key fallback |
| `VONAGE_API_SECRET` | - | Vonage API secret fallback |
| `VONAGE_BASE_URL` | `https://rest.nexmo.com` | Vonage REST API base URL |
| `SEVEN_API_KEY` | - | seven.io API key fallback |
| `SEVEN_BASE_URL` | `https://gateway.seven.io/api` | seven.io API base URL |
| `STRIPE_SECRET_KEY` | - | Stripe secret key fallback |
| `STRIPE_BASE_URL` | `https://api.stripe.com/v1` | Stripe API base URL |
| `SHOPIFY_SHOP` | - | Shopify shop subdomain or myshopify.com host fallback |
| `SHOPIFY_ACCESS_TOKEN` | - | Shopify Admin API access token fallback |
| `SHOPIFY_API_KEY` | - | Legacy Shopify API key fallback |
| `SHOPIFY_PASSWORD` | - | Legacy Shopify Admin API password fallback |
| `SHOPIFY_API_VERSION` | `2026-01` | Shopify Admin REST API version |
| `SHOPIFY_BASE_URL` | - | Shopify Admin REST API base URL override |
| `WOOCOMMERCE_URL` | - | WooCommerce site URL fallback |
| `WOOCOMMERCE_BASE_URL` | - | WooCommerce REST API base URL override |
| `WOOCOMMERCE_CONSUMER_KEY` | - | WooCommerce consumer key fallback |
| `WOOCOMMERCE_CONSUMER_SECRET` | - | WooCommerce consumer secret fallback |
| `CHARGEBEE_API_KEY` | - | Chargebee API key fallback |
| `CHARGEBEE_SITE` | - | Chargebee site subdomain fallback |
| `CHARGEBEE_BASE_URL` | - | Chargebee API base URL override |
| `PADDLE_VENDOR_ID` | - | Paddle vendor ID fallback |
| `PADDLE_VENDOR_AUTH_CODE` | - | Paddle vendor auth code fallback |
| `PADDLE_SANDBOX` | `false` | Use Paddle sandbox vendor API |
| `PADDLE_BASE_URL` | - | Paddle vendor API base URL override |
| `PROFITWELL_API_TOKEN` | - | ProfitWell API token fallback |
| `PROFITWELL_BASE_URL` | `https://api.profitwell.com/v2` | ProfitWell API base URL |
| `TAPFILIATE_API_KEY` | - | Tapfiliate API key fallback |
| `TAPFILIATE_BASE_URL` | `https://api.tapfiliate.com/1.6` | Tapfiliate API base URL |
| `PUSHBULLET_ACCESS_TOKEN` | - | Pushbullet access token fallback |
| `PUSHBULLET_BASE_URL` | `https://api.pushbullet.com/v2` | Pushbullet API base URL |
| `PUSHCUT_API_KEY` | - | Pushcut API key fallback |
| `PUSHCUT_BASE_URL` | `https://api.pushcut.io/v1` | Pushcut API base URL |
| `GOTIFY_BASE_URL` | - | Gotify server base URL fallback |
| `GOTIFY_APP_TOKEN` | - | Gotify application token fallback |
| `GOTIFY_CLIENT_TOKEN` | - | Gotify client token fallback |
| `PUSHOVER_API_TOKEN` | - | Pushover application API token fallback |
| `PUSHOVER_USER_KEY` | - | Pushover user or group key fallback |
| `PUSHOVER_BASE_URL` | `https://api.pushover.net/1` | Pushover API base URL |
| `SIGNL4_TEAM_SECRET` | - | SIGNL4 team secret fallback |
| `SIGNL4_WEBHOOK_URL` | - | SIGNL4 full webhook URL fallback |
| `SIGNL4_BASE_URL` | `https://connect.signl4.com/webhook` | SIGNL4 webhook base URL |
| `WORDPRESS_URL` | - | WordPress site URL fallback |
| `WORDPRESS_USERNAME` | - | WordPress username fallback |
| `WORDPRESS_PASSWORD` | - | WordPress application password fallback |
| `STRAPI_URL` | - | Strapi API base URL fallback |
| `STRAPI_API_TOKEN` | - | Strapi API token fallback |
| `STRAPI_EMAIL` | - | Strapi local-auth email fallback |
| `STRAPI_PASSWORD` | - | Strapi local-auth password fallback |
| `STRAPI_API_VERSION` | `v4` | Strapi REST API version hint |
| `CONTENTFUL_SPACE_ID` | - | Contentful space ID fallback |
| `CONTENTFUL_DELIVERY_TOKEN` | - | Contentful delivery access token fallback |
| `CONTENTFUL_PREVIEW_TOKEN` | - | Contentful preview access token fallback |
| `CONTENTFUL_BASE_URL` | `https://cdn.contentful.com` | Contentful Delivery API base URL |
| `CONTENTFUL_PREVIEW_BASE_URL` | `https://preview.contentful.com` | Contentful Preview API base URL |
| `GHOST_URL` | - | Ghost site URL fallback |
| `GHOST_CONTENT_API_KEY` | - | Ghost Content API key fallback |
| `GHOST_ADMIN_API_KEY` | - | Ghost Admin API key fallback |
| `GHOST_API_VERSION` | `v5.0` | Ghost API version header |
| `STORYBLOK_CONTENT_TOKEN` | - | Storyblok Content API token fallback |
| `STORYBLOK_MANAGEMENT_TOKEN` | - | Storyblok Management API token fallback |
| `STORYBLOK_SPACE_ID` | - | Storyblok space ID fallback |
| `STORYBLOK_CONTENT_BASE_URL` | `https://api.storyblok.com/v2/cdn` | Storyblok Content API base URL |
| `STORYBLOK_MANAGEMENT_BASE_URL` | `https://mapi.storyblok.com/v1` | Storyblok Management API base URL |
| `NETLIFY_ACCESS_TOKEN` | - | Netlify access token fallback |
| `NETLIFY_BASE_URL` | `https://api.netlify.com/api/v1` | Netlify API base URL |
| `UPTIMEROBOT_API_KEY` | - | UptimeRobot API key fallback |
| `UPTIMEROBOT_BASE_URL` | `https://api.uptimerobot.com/v2` | UptimeRobot API base URL |
| `PAGERDUTY_API_TOKEN` | - | PagerDuty API token fallback |
| `PAGERDUTY_FROM_EMAIL` | - | PagerDuty From email fallback |
| `PAGERDUTY_BASE_URL` | `https://api.pagerduty.com` | PagerDuty API base URL |
| `SENTRY_AUTH_TOKEN` | - | Sentry auth token fallback |
| `SENTRY_BASE_URL` | `https://sentry.io` | Sentry API base URL |
| `CLOUDFLARE_API_TOKEN` | - | Cloudflare API token fallback |
| `CLOUDFLARE_BASE_URL` | `https://api.cloudflare.com/client/v4` | Cloudflare API base URL |
| `GRAFANA_API_TOKEN` | - | Grafana API token fallback |
| `GRAFANA_BASE_URL` | - | Grafana base URL fallback |
| `METABASE_BASE_URL` | - | Metabase base URL fallback |
| `METABASE_SESSION_TOKEN` | - | Metabase session token fallback |
| `METABASE_API_KEY` | - | Metabase API key fallback |
| `METABASE_USERNAME` | - | Metabase username fallback |
| `METABASE_PASSWORD` | - | Metabase password fallback |
| `ELASTICSEARCH_BASE_URL` | - | Elasticsearch base URL fallback |
| `ELASTICSEARCH_API_KEY` | - | Elasticsearch API key fallback |
| `ELASTICSEARCH_BEARER_TOKEN` | - | Elasticsearch bearer token fallback |
| `ELASTICSEARCH_USERNAME` | - | Elasticsearch username fallback |
| `ELASTICSEARCH_PASSWORD` | - | Elasticsearch password fallback |
| `ELASTICSEARCH_IGNORE_SSL_ISSUES` | `false` | Skip Elasticsearch SSL verification |
| `SPLUNK_BASE_URL` | - | Splunk management API base URL fallback |
| `SPLUNK_AUTH_TOKEN` | - | Splunk auth token fallback |
| `SPLUNK_ALLOW_UNAUTHORIZED_CERTS` | `false` | Allow self-signed Splunk certificates |
| `URLSCAN_API_KEY` | - | urlscan.io API key fallback |
| `URLSCAN_BASE_URL` | `https://urlscan.io/api/v1` | urlscan.io API base URL |
| `HUNTER_API_KEY` | - | Hunter API key fallback |
| `HUNTER_BASE_URL` | `https://api.hunter.io/v2` | Hunter API base URL |
| `MAILCHECK_API_KEY` | - | Mailcheck API key fallback |
| `MAILCHECK_BASE_URL` | `https://api.mailcheck.co/v1` | Mailcheck API base URL |
| `PEEKALINK_API_KEY` | - | Peekalink API key fallback |
| `PEEKALINK_BASE_URL` | `https://api.peekalink.io` | Peekalink API base URL |
| `JINA_API_KEY` | - | Jina AI API key fallback |
| `JINA_READER_BASE_URL` | `https://r.jina.ai` | Jina Reader API base URL |
| `JINA_SEARCH_BASE_URL` | `https://s.jina.ai` | Jina Search API base URL |
| `JINA_DEEPSEARCH_BASE_URL` | `https://deepsearch.jina.ai/v1` | Jina DeepSearch API base URL |
| `MISP_BASE_URL` | - | MISP base URL fallback |
| `MISP_API_KEY` | - | MISP API key fallback |
| `MISP_ALLOW_UNAUTHORIZED_CERTS` | ignored | Deprecated. TLS verification is always enforced |
| `THEHIVE_BASE_URL` | - | TheHive base URL fallback |
| `THEHIVE_API_KEY` | - | TheHive API key fallback |
| `THEHIVE_API_VERSION` | `v1` | TheHive API version hint |
| `THEHIVE_ALLOW_UNAUTHORIZED_CERTS` | ignored | Deprecated. TLS verification is always enforced |
| `SECURITYSCORECARD_API_KEY` | - | SecurityScorecard API key fallback |
| `SECURITYSCORECARD_BASE_URL` | `https://api.securityscorecard.io` | SecurityScorecard API base URL |
| `ELASTIC_SECURITY_BASE_URL` | - | Elastic Security Kibana base URL fallback |
| `ELASTIC_SECURITY_API_KEY` | - | Elastic Security API key fallback |
| `ELASTIC_SECURITY_USERNAME` | - | Elastic Security basic-auth username fallback |
| `ELASTIC_SECURITY_PASSWORD` | - | Elastic Security basic-auth password fallback |
| `BASEROW_API_TOKEN` | - | Baserow API or database token fallback |
| `BASEROW_BASE_URL` | `https://api.baserow.io` | Baserow API base URL |
| `SUPABASE_URL` | - | Supabase project URL fallback |
| `SUPABASE_SERVICE_ROLE_KEY` | - | Supabase service role key fallback |
| `SUPABASE_API_KEY` | - | Supabase anon/API key fallback |
| `SUPABASE_BASE_URL` | - | Supabase REST API base URL override |
| `QUICKBASE_HOSTNAME` | - | Quickbase realm hostname fallback |
| `QUICKBASE_USER_TOKEN` | - | Quickbase user token fallback |
| `QUICKBASE_BASE_URL` | `https://api.quickbase.com/v1` | Quickbase API base URL |
| `SEATABLE_API_TOKEN` | - | SeaTable API token fallback |
| `SEATABLE_BASE_URL` | `https://cloud.seatable.io` | SeaTable API base URL |
| `STACKBY_API_KEY` | - | Stackby API key fallback |
| `STACKBY_BASE_URL` | `https://stackby.com/api/betav1` | Stackby API base URL |
| `NOCODB_API_TOKEN` | - | NocoDB API or user token fallback |
| `NOCODB_BASE_URL` | `https://app.nocodb.com` | NocoDB API base URL |
| `NOCODB_AUTH_HEADER` | `xc-token` | NocoDB auth header name, `xc-token` or `xc-auth` |
| `CODA_API_TOKEN` | - | Coda API token fallback |
| `CODA_BASE_URL` | `https://coda.io/apis/v1` | Coda API base URL |
| `GRIST_API_KEY` | - | Grist API key fallback |
| `GRIST_BASE_URL` | `https://docs.getgrist.com/api` | Grist API base URL |
| `ADALO_API_KEY` | - | Adalo API key fallback |
| `ADALO_APP_ID` | - | Adalo app ID fallback |
| `ADALO_BASE_URL` | - | Adalo app API base URL override |
| `BUBBLE_API_TOKEN` | - | Bubble Data API token fallback |
| `BUBBLE_APP_NAME` | - | Bubble app name fallback |
| `BUBBLE_ENVIRONMENT` | `live` | Bubble environment |
| `BUBBLE_DOMAIN` | - | Bubble custom/self-hosted domain fallback |
| `BUBBLE_BASE_URL` | - | Bubble API base URL override |
| `COCKPIT_BASE_URL` | - | Cockpit site or API base URL fallback |
| `COCKPIT_ACCESS_TOKEN` | - | Cockpit access token fallback |
| `TELEGRAM_API_BASE_URL` | `https://api.telegram.org` | Telegram Bot API base URL |
| `WHATSAPP_ACCESS_TOKEN` | - | WhatsApp Business Cloud access token fallback |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | - | WhatsApp business account ID fallback |
| `WHATSAPP_PHONE_NUMBER_ID` | - | WhatsApp sender phone number ID fallback |
| `WHATSAPP_BASE_URL` | `https://graph.facebook.com/v19.0` | WhatsApp Graph API base URL |
| `WHATSAPP_WEBHOOK_VERIFY_TOKEN` | - | WhatsApp Cloud webhook challenge token |
| `WHATSAPP_APP_SECRET` | - | Required Meta app secret for webhook signature verification |
| `WHATSAPP_SHOW_TOOL_EVENTS` | `false` | Show compact tool-call events in WhatsApp bot replies |
| `DISCORD_BASE_URL` | `https://discord.com/api/v10` | Discord REST API base URL |
| `TEAMS_BOT_APP_ID` | - | Bot Framework app ID for Teams webhook replies |
| `TEAMS_BOT_APP_PASSWORD` | - | Bot Framework client secret for Teams webhook replies |
| `TEAMS_BOT_TENANT_ID` | - | Optional Azure tenant ID used during setup |
| `TEAMS_BOT_RESPOND_MODE` | `mention` | Teams group/channel behavior: `mention` or `all` |
| `TEAMS_BOT_VALIDATE_AUTH` | ignored | Deprecated. Bot Framework bearer-token validation is always enforced |
| `TEAMS_BOT_SHOW_TOOL_EVENTS` | `false` | Show compact Teams tool call/result messages during streamed replies |
| `TEAMS_BOT_TOKEN_URL` | `https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token` | Bot Framework OAuth token URL |
| `TEAMS_BOT_OPENID_CONFIG_URL` | `https://login.botframework.com/v1/.well-known/openidconfiguration` | Bot Framework OpenID metadata URL |
| `GOOGLE_BOOKS_API_KEY` | - | Optional Google Books API key fallback |
| `GOOGLE_BOOKS_BASE_URL` | `https://www.googleapis.com/books/v1` | Google Books API base URL |
| `YOUTUBE_API_KEY` | - | YouTube Data API key fallback |
| `YOUTUBE_BASE_URL` | `https://www.googleapis.com/youtube/v3` | YouTube Data API base URL |
| `SPOTIFY_ACCESS_TOKEN` | - | Optional Spotify bearer token fallback |
| `SPOTIFY_CLIENT_ID` | - | Spotify client ID for client-credentials catalog tools |
| `SPOTIFY_CLIENT_SECRET` | - | Spotify client secret for client-credentials catalog tools |
| `SPOTIFY_BASE_URL` | `https://api.spotify.com/v1` | Spotify Web API base URL |
| `SPOTIFY_ACCOUNTS_BASE_URL` | `https://accounts.spotify.com` | Spotify Accounts API base URL |
| `REDDIT_ACCESS_TOKEN` | - | Optional Reddit OAuth bearer token fallback |
| `REDDIT_REFRESH_TOKEN` | - | Optional Reddit OAuth refresh token fallback |
| `REDDIT_CLIENT_ID` | - | Reddit OAuth client ID fallback |
| `REDDIT_CLIENT_SECRET` | - | Reddit OAuth client secret fallback |
| `REDDIT_BASE_URL` | `https://oauth.reddit.com` | Reddit OAuth API base URL |
| `REDDIT_PUBLIC_BASE_URL` | `https://www.reddit.com` | Reddit public JSON API base URL |
| `REDDIT_TOKEN_URL` | `https://www.reddit.com/api/v1/access_token` | Reddit OAuth token URL |
| `DISCOURSE_API_KEY` | - | Discourse API key fallback |
| `DISCOURSE_API_USERNAME` | - | Discourse API username fallback |
| `DISCOURSE_BASE_URL` | - | Discourse forum base URL |
| `MEDIUM_ACCESS_TOKEN` | - | Medium access token fallback |
| `MEDIUM_BASE_URL` | `https://api.medium.com/v1` | Medium API base URL |
| `BAMBOOHR_API_KEY` | - | BambooHR API key fallback |
| `BAMBOOHR_SUBDOMAIN` | - | BambooHR company subdomain fallback |
| `BAMBOOHR_BASE_URL` | `https://api.bamboohr.com/api/gateway.php` | BambooHR API gateway base URL |
| `BEEMINDER_ACCESS_TOKEN` | - | Beeminder auth token fallback |
| `BEEMINDER_BASE_URL` | `https://www.beeminder.com/api/v1` | Beeminder API base URL |
| `CLOCKIFY_API_KEY` | - | Clockify API key fallback |
| `CLOCKIFY_BASE_URL` | `https://api.clockify.me/api/v1` | Clockify API base URL |
| `HARVEST_ACCESS_TOKEN` | - | Harvest access token fallback |
| `HARVEST_ACCOUNT_ID` | - | Harvest account ID fallback |
| `HARVEST_BASE_URL` | `https://api.harvestapp.com/v2` | Harvest API base URL |
| `OURA_ACCESS_TOKEN` | - | Oura access token fallback |
| `OURA_BASE_URL` | `https://api.ouraring.com/v2` | Oura API base URL |
| `STRAVA_ACCESS_TOKEN` | - | Strava access token fallback |
| `STRAVA_BASE_URL` | `https://www.strava.com/api/v3` | Strava API base URL |
| `HOMEASSISTANT_ACCESS_TOKEN` | - | Home Assistant long-lived token fallback |
| `HOMEASSISTANT_BASE_URL` | - | Home Assistant API base URL |
| `PHILIPS_HUE_ACCESS_TOKEN` | - | Philips Hue access token fallback |
| `PHILIPS_HUE_USERNAME` | - | Philips Hue bridge username fallback |
| `PHILIPS_HUE_BASE_URL` | `https://api.meethue.com/route` | Philips Hue routed API base URL |
| `ACTIVECAMPAIGN_API_KEY` | - | ActiveCampaign API key fallback |
| `ACTIVECAMPAIGN_BASE_URL` | - | ActiveCampaign account API URL |
| `CONVERTKIT_API_SECRET` | - | ConvertKit API secret fallback |
| `CONVERTKIT_BASE_URL` | `https://api.convertkit.com/v3` | ConvertKit API base URL |
| `GETRESPONSE_API_KEY` | - | GetResponse API key fallback |
| `GETRESPONSE_BASE_URL` | `https://api.getresponse.com/v3` | GetResponse API base URL |
| `MAILERLITE_API_KEY` | - | MailerLite API key fallback |
| `MAILERLITE_BASE_URL` | `https://connect.mailerlite.com/api` | MailerLite API base URL |
| `MAILERLITE_CLASSIC_API` | `false` | Use MailerLite Classic API authentication/header style |
| `ACTIONNETWORK_API_KEY` | - | Action Network API key fallback |
| `ACTIONNETWORK_BASE_URL` | `https://actionnetwork.org/api/v2` | Action Network API base URL |
| `AUTOPILOT_API_KEY` | - | Autopilot API key fallback |
| `AUTOPILOT_BASE_URL` | `https://api2.autopilothq.com/v1` | Autopilot API base URL |
| `EGOI_API_KEY` | - | E-goi API key fallback |
| `EGOI_BASE_URL` | `https://api.egoiapp.com` | E-goi API base URL |
| `VERO_AUTH_TOKEN` | - | Vero auth token fallback |
| `VERO_BASE_URL` | `https://api.getvero.com/api/v2` | Vero API base URL |
| `CUSTOMERIO_TRACKING_SITE_ID` | - | Customer.io tracking site ID fallback |
| `CUSTOMERIO_TRACKING_API_KEY` | - | Customer.io tracking API key fallback |
| `CUSTOMERIO_APP_API_KEY` | - | Customer.io app API key fallback |
| `CUSTOMERIO_REGION` | `track.customer.io` | Customer.io tracking region host |
| `CUSTOMERIO_TRACKING_BASE_URL` | - | Customer.io tracking API base URL override |
| `CUSTOMERIO_APP_BASE_URL` | - | Customer.io app API base URL override |
| `ITERABLE_API_KEY` | - | Iterable API key fallback |
| `ITERABLE_BASE_URL` | `https://api.iterable.com/api` | Iterable API base URL |
| `POSTHOG_API_KEY` | - | PostHog project API key fallback |
| `POSTHOG_BASE_URL` | `https://app.posthog.com` | PostHog API base URL |
| `SEGMENT_WRITE_KEY` | - | Segment write key fallback |
| `SEGMENT_BASE_URL` | `https://api.segment.io/v1` | Segment tracking API base URL |
| `COPPER_API_KEY` | - | Copper API key fallback |
| `COPPER_EMAIL` | - | Copper user email fallback |
| `COPPER_BASE_URL` | `https://api.copper.com/developer_api/v1` | Copper API base URL |
| `AGILECRM_EMAIL` | - | Agile CRM account email fallback |
| `AGILECRM_API_KEY` | - | Agile CRM API key fallback |
| `AGILECRM_SUBDOMAIN` | - | Agile CRM account subdomain fallback |
| `AGILECRM_BASE_URL` | - | Agile CRM API base URL override |
| `MONICA_ACCESS_TOKEN` | - | Monica CRM API token fallback |
| `MONICA_BASE_URL` | `https://app.monicahq.com/api` | Monica CRM API base URL |

### Log File Rotation

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_LOG_FILE` | `service.log` | Rotating log filename (all modes) |
| `SERVICE_LOG_MAX_BYTES` | `10485760` | Rotate log after this many bytes |
| `SERVICE_LOG_BACKUP_COUNT` | `5` | Number of rotated log backups to keep |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `AUDIT_LOG_ENABLED` | `true` | Log redacted HTTP/API primitive tool events to audit log |

### HTTP Tool Egress Policy

These settings apply to `http_request`, `api_discover`, saved custom HTTP
tools, browser navigation/fallback fetches, RSS trigger polling, MCP registry
and bundle downloads, MCP HTTP transports, OpenAI image URL fallback fetches,
and supported service-integration base URLs. The model cannot override them per
call.

| Variable | Default | Description |
|----------|---------|-------------|
| `HTTP_INTERNAL_ALLOWLIST` | - | Comma-separated exact internal hosts or `host:port` pairs that HTTP tools may reach, e.g. `host.docker.internal:1420,homeassistant.local:8123` |
| `HTTP_DOMAIN_ALLOWLIST` | - | Optional comma-separated public-domain allowlist. Supports exact domains and `*.example.com` wildcards. Empty means public domains are allowed unless blocked. |
| `HTTP_DOMAIN_BLOCKLIST` | - | Comma-separated public-domain blocklist. Supports exact domains and `*.example.com` wildcards. |
| `HTTP_MAX_REDIRECTS` | `5` | Maximum redirects followed by HTTP tools (0-20) |
| `HTTP_ALLOW_HTTPS_TO_HTTP_REDIRECT` | `false` | Whether HTTP tools may follow redirects from `https://` to `http://` |

By default these egress paths are public-internet-only. Loopback, private,
link-local, reserved, unspecified, multicast, and metadata targets are blocked
after DNS resolution unless the target is a non-metadata host explicitly listed
in `HTTP_INTERNAL_ALLOWLIST`. Service-integration base URL validation also
blocks literal private/metadata targets and honors the domain allow/block lists.
Policy-managed requests pin the request-time socket resolver to the IPs that
passed DNS policy validation, preserving hostname-based TLS validation while
closing DNS-rebinding gaps.

### Autonomous Operation

| Variable | Default | Description |
|----------|---------|-------------|
| `TICKER_POLL_INTERVAL` | `5` | Seconds between polls for due tasks (1-60) |
| `MAX_CONCURRENT_AUTONOMOUS` | `5` | Max concurrent autonomous tasks (`0` = unlimited) |
| `MAX_CONCURRENT_INTERACTIVE` | `0` | Global ceiling on concurrent interactive chat turns across all users in the API process (`0` = unlimited, feature off). When saturated, a chat request that would start a new turn is rejected with HTTP `429` + `Retry-After` after the bounded wait below; prompts to already-busy threads and `is_self_invoke` relay turns are exempt (they start no new concurrency / are bounded by `MAX_CONCURRENT_AUTONOMOUS`). 4-8 is a reasonable starting value on small hosts. See "Capacity shedding" in `api.md`. |
| `INTERACTIVE_ADMISSION_WAIT_SECONDS` | `10` | Seconds an over-capacity interactive chat request may wait for a free turn slot before the `429` (0-120; `0` = reject immediately). Only meaningful when `MAX_CONCURRENT_INTERACTIVE` > 0. |
| `LOCK_TIMEOUT` | `120` | Seconds to wait on per-thread lock before timing out |
| `DEFAULT_EXECUTOR_MAX_WORKERS` | `32` | Worker-thread ceiling for the asyncio default executor in the API process, which carries nearly all `to_thread` blocking work (integrations, voice, OAuth, credential probes, non-streaming turns). The stock asyncio size is only `min(32, cores + 4)` (8 on a 4-core host); threads here are cheap blocking-I/O waiters, so size for concurrency, not cores (8-256) |
| `AGENT_MAX_ITERATIONS` | `500` | Max agent loop iterations per turn (10-10,000); a safety backstop, not a tuning knob. Callable threads use their own per-thread cap |
| `TOOL_TIMEOUT` | `300` | Max seconds a tool or callable-thread invocation may run |
| `TOOL_OUTPUT_MAX_CHARS` | `100000` | Max characters stored for one tool result. Larger outputs keep the first ~75k and last ~25k characters with a truncation marker. |
| `TOOL_TIMING_IN_RESULTS` | `false` | Append each tool result's server-measured duration (for example `[Duration: 3.4s]`) to the result text the model sees. Costs a few tokens per tool call; gives the agent execution-time context. Timing is always emitted on the `tool_call`/`tool_result` SSE events and persisted for history reload regardless of this flag. |
| `BASH_ENV_PASSTHROUGH` | `` | Comma-separated extra environment variable NAMES exposed to `bash_execute` commands beyond the base allowlist (`PATH, HOME, LANG, LC_ALL, TMPDIR`). Commands run under a deny-by-default scrubbed environment so backend secrets never leak into command output; name only what a command genuinely needs, never secret-bearing vars. Deployments behind an egress proxy or custom CA, or using a venv, typically need e.g. `HTTPS_PROXY,SSL_CERT_FILE,REQUESTS_CA_BUNDLE,VIRTUAL_ENV`; toolchain homes like `JAVA_HOME,GOPATH` are other common picks. |
| `SEQUENTIAL_TOOL_EXECUTION` | `false` | Run a turn's tool calls one at a time in the order the model emitted them, instead of concurrently. Global default; overridable per-thread (a thread can force on or off, or inherit this). Slower for independent calls but avoids parallel-execution races. The `run_tools_in_order` tool still orders a single batch even when this is off. |
| `HOOKS_ENABLED` | `true` | Master kill switch for lifecycle hooks. When off, no hook fires on any thread (a debug/escape hatch). Global default; overridable per-thread. Sits on top of each hook's own `enabled` flag and any per-thread per-hook override. See `docs/agent-systems/hooks.md`. |
| `HOOK_MUTATE_POOL_WORKERS` | `4` | Thread-pool size for running sync mutate-plane hooks (the `pre_tool_use` / `post_tool_use` guardrails). Scoped separately from the observe pool so a slow side-effect hook cannot starve a guardrail. Clamped to a floor of 1. |
| `HOOK_OBSERVE_POOL_WORKERS` | `4` | Thread-pool size for running sync observe-plane hooks (the `notify` / `create_todo` / `webhook` side effects). Separate from the mutate pool. Clamped to a floor of 1. |
| `HOOK_OBSERVE_DISPATCH_WORKERS` | `2` | Thread-pool size for the off-turn observe *dispatchers* on the no-loop (sync) path. Observe hooks are fire-and-forget: the fire point schedules the dispatch and returns immediately (a background loop task when a loop is running, else this pool). Dedicated so a dispatcher waiting on `HOOK_OBSERVE_POOL_WORKERS` cannot starve the hooks it dispatches. Clamped to a floor of 1. |
| `HOOKS_RUN_COMMAND_ENABLED` | `false` | Deployment gate for the `run_command` hook action (a hook that shells out on the host). Off by default. Even when on, only an admin account may author a `run_command` hook; the gate is enforced at authoring on every surface AND again at execution. Leave off unless you trust every admin with host shell access. See `docs/agent-systems/hooks.md`. |

### Context Management

Nymeria automatically manages conversation context to prevent overflow. The default `auto_compact` mode summarizes conversations when approaching the model's context limit.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_MANAGEMENT` | `auto_compact` | Strategy: `auto_compact`, `sliding_window`, or `none` |
| `COMPACT_THRESHOLD_MODE` | `tokens` | Trigger mode: `tokens` (absolute input-token count, default) or `percentage` (of context window) |
| `COMPACT_THRESHOLD` | `0.8` | Used when `COMPACT_THRESHOLD_MODE=percentage`. Trigger compaction at this fraction of the model context window (0.05-0.95). Example: `0.38` is about 400k tokens on GPT-5.5's 1.05M window. |
| `COMPACT_THRESHOLD_TOKENS` | `200000` | Used when `COMPACT_THRESHOLD_MODE=tokens`. Trigger compaction at this absolute input-token count (1,000-2,000,000). Clamped to the model's context window at runtime. Token counts come from the most recent provider response (`usage_metadata`), not character estimates. |
| `COMPACT_KEEP_MESSAGES` | `4` | Minimum messages before compaction is allowed |
| `COMPACT_MODEL` | (main model) | Reserved: accepted and persisted, but not consumed by the runtime yet (summarization always runs on the thread's own model) |
| `COMPACT_PROACTIVE_ENABLED` | `false` | Opt-in: compact idle threads in the background once they sit near the auto-compact trigger, while the provider prompt cache is still warm (cheap summary input). Per-thread override via `ThreadLLMConfig.compact_proactive_enabled`. See `docs/agent-systems/compaction-and-checkpoints.md` |
| `COMPACT_PROACTIVE_IDLE_SECONDS` | `210` | Idle time after a turn end before a proactive compaction may fire (30-3600). Keep it inside the provider's cache TTL or the cost benefit vanishes |
| `COMPACT_PROACTIVE_MIN_PCT` | `85` | Occupancy floor: proactive compaction fires only once context usage reaches this percentage of the auto-compact trigger (10-100) |
| `SLIDING_WINDOW_CYCLES` | `5` | Legacy: cycles to keep when using `sliding_window` mode |

**Context Management Modes:**

- **`auto_compact`** (default): When token usage reaches the threshold, Nymeria:
  1. Asks the agent to summarize the conversation (it already has full context)
  2. Agent saves important facts to persistent memory via `memory_add(scope="global", ...)`
  3. Clears the conversation and persists a visible compaction notice with the summary
  4. On async `/chat` streams, compacts before the next provider call when prior usage already crossed the trigger; post-turn async compaction emits `compacting` after compaction starts, then `compacted`, and streams the resumed assistant continuation. Sync/manual paths attach the summary to the next user message.

- **`sliding_window`**: Legacy mode that simply removes old messages, keeping the last N cycles

- **`none`**: No automatic context management (manual `/compact` still available)

### Notification Routing (Destinations + Profiles)

Nymeria's notification system is driven by user-configured **destinations**
(concrete delivery targets) and **profiles** (named bundles) stored in
`data/accounts.db`, not by environment variables. The env vars listed under
the Telegram / Discord / Slack / Teams sections below are used ONLY for
auto-seeding default destinations on first run so existing deployments keep
working without reconfiguration.

To configure destinations interactively, use **Settings → Notifications** in
the desktop app, or have the agent do it via the
`nymeria_notification_destination_*` MCP tools. See
[`notifications.md`](notifications.md) for the data model, channel-type
registry, and REST API.

### Watchdog, TODO, and Push Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCHDOG_ENABLED` | `true` | Enable the watchdog ticker sub-loop that monitors TODO staleness |
| `WATCHDOG_INTERVAL_MINUTES` | `5` | Minutes between watchdog checks (1-60) |
| `TODO_STALENESS_MINUTES` | `20` | Minutes without update before TODO is stale (5-1440) |
| `TODO_AUTO_ARCHIVE_DAYS` | `7` | Days after completion before the ticker removes completed TODOs from the active TODO JSON list (1-30) |
| `ACTIVITY_RETENTION_HOURS` | `12` | Hours to retain activity log entries (1-168) |
| `DREAM_DEFAULT_MIN_INTERVAL_HOURS` | `6` | Default shortest gap between dreams; a thread inherits this when its `dreaming.min_interval_hours` is blank (1-168) |
| `DREAM_DEFAULT_MIN_IDLE_MINUTES` | `30` | Default idle time before a dream may start; inherited when `dreaming.min_idle_minutes` is blank (5-10080) |
| `DREAM_DEFAULT_MIN_TURNS_SINCE_LAST` | `10` | Default new user turns required since the last dream; inherited when `dreaming.min_turns_since_last` is blank (1-10000) |
| `DREAM_DEFAULT_MODEL` | - | Default model for dream turns; inherited when `dreaming.model` is blank, else the global active model is used |
| `FCM_ENABLED` | `false` | Enable Firebase Cloud Messaging push notifications |
| `FCM_CREDENTIALS_JSON` | - | Path to Firebase service account JSON |
| `NYMERIA_WATCHDOG_DISABLED` | - | Set to `1` / `true` / `yes` at runtime to mute the watchdog sweep without restarting (re-read every cycle). See also the file flag below. |

The private server-side FCM credential is an operator-provided JSON file (for
example `Nymeria/firebase-service-account.json`). It is supplied out of band,
never committed to the repository, and wired into Docker via
`FCM_CREDENTIALS_JSON`.

**Watchdog runtime kill switches** (disable without restart):

- **Env var**: `NYMERIA_WATCHDOG_DISABLED=1` (re-read on every sweep cycle)
- **File flag**: `{data_dir}/flags/watchdog-off`  -  persistent across container restarts because `/data` is a Docker volume. Create it with `docker exec nymeria-worker touch /data/flags/watchdog-off`; remove with `rm` to re-enable.

The watchdog is a supervisory sub-loop of the ticker (`core/watchdog_sweep.py`): in Docker it runs inside the `nymeria-worker` container, in slim inside the single process. There is no separate watchdog container or `run.py watchdog` subcommand. To disable it entirely, set `WATCHDOG_ENABLED=false` and restart the worker (or slim process). See `docs/architecture.md` section 4.1 for details.

### Voice (TTS / STT)

| Variable | Default | Description |
|----------|---------|-------------|
| `TTS_PROVIDER` | `none` | TTS provider: `none`, `openai`, `kokoro` (local), `qwen3` (local GPU), `gemini`, `cartesia`, `elevenlabs`, `edge` (free, keyless) |
| `TTS_BASE_URL` | (per provider) | TTS API base URL. Not used for Gemini, Cartesia, ElevenLabs, or Edge. OpenAI defaults to `https://api.openai.com/v1`; `kokoro` unset runs in-process (`nymeriaos[voice-local]` extra), set it to use the speaches sidecar (`http://speaches:8000/v1` in the Docker stack); `qwen3` defaults to `http://localhost:8880/v1` |
| `TTS_API_KEY` | (falls back to `OPENAI_API_KEY`) | API key for hosted TTS. Gemini uses `GEMINI_API_KEY`; Cartesia and ElevenLabs require `TTS_API_KEY` set to their own key. Local providers (kokoro, qwen3) need no key |
| `TTS_MODEL` | (per provider) | Defaults when unset: OpenAI `gpt-4o-mini-tts`; Cartesia `sonic-3.5`; ElevenLabs `eleven_flash_v2_5`; Gemini `gemini-3.1-flash-tts-preview`; Qwen3 `Qwen3-TTS-0.6B`; kokoro via speaches `speaches-ai/Kokoro-82M-v1.0-ONNX` |
| `TTS_VOICE` | (per provider) | Defaults when unset: OpenAI `nova` (also `alloy`, `echo`, `fable`, `onyx`, `shimmer`); kokoro `af_heart`; Gemini `Kore` (also `Puck`, `Charon`, 30 total); Edge `en-US-AriaNeural`; ElevenLabs Rachel. Cartesia has no default: set a voice UUID from play.cartesia.ai |
| `TTS_OUTPUT_FORMAT` | `mp3` | Output format: mp3, wav, opus, aac. Gemini, Cartesia, and Edge always return MP3 |
| `TTS_SPEED` | `1.0` | Playback speed 0.25-4.0; Cartesia clamps to 0.6-1.5, ElevenLabs to 0.7-1.2, Edge to 0.5-2.0; not applicable for Gemini, and OpenAI's `gpt-4o-mini-tts` accepts but ignores it |
| `STT_PROVIDER` | `none` | STT provider: `none`, `openai`, `groq`, `faster-whisper` (local) |
| `STT_BASE_URL` | (per provider) | STT API base URL. `faster-whisper` unset runs in-process (`nymeriaos[voice-local]` extra), set it to use the speaches sidecar |
| `STT_API_KEY` | (falls back to `OPENAI_API_KEY`) | API key for hosted STT. Groq also reads `GROQ_API_KEY`. Local faster-whisper needs no key |
| `GROQ_API_KEY` | - | Groq key (STT at roughly $0.04 per audio hour; shared with the Groq LLM provider) |
| `STT_MODEL` | (per provider) | Defaults when unset: OpenAI `gpt-4o-mini-transcribe`; Groq `whisper-large-v3-turbo`; in-process faster-whisper `small` (CPU-sized); via speaches `Systran/faster-whisper-small` |
| `STT_LANGUAGE` | - | Language hint (ISO 639-1, e.g., `en`) |
| `VOICE_DEFAULT_THREAD_ID` | - | Default thread for voice/watch interactions (falls back to `watch-default`) |

Local voice has two shapes. Bare-metal installs run the engines in-process via
the `nymeriaos[voice-local]` extra (kokoro-onnx + faster-whisper, CPU-friendly;
model weights download on first use into `data/voice/`). The Docker full stack
runs the `speaches` sidecar instead (`--profile voice`, digest-pinned CPU
image, published on `127.0.0.1:8970` only): one container serves both
faster-whisper STT and Kokoro TTS over OpenAI-compatible endpoints, and the
init wizard points `TTS_BASE_URL`/`STT_BASE_URL` at it. Pull its models once
with `uvx speaches-cli` (see the compose comments). The GPU-tier `qwen3-tts`
sidecar moved to `--profile voice-gpu` and still requires an operator-pinned
`QWEN3_TTS_IMAGE` digest (the compose default is an invalid placeholder).

**Gemini TTS** requires `GEMINI_API_KEY` (also used for document extraction). Supports 200+ inline audio tags for expressive speech  -  e.g., `[whispers]`, `[excitedly]`, `[sighs]`. See [Gemini TTS prompting guide](https://ai.google.dev/gemini-api/docs/speech-generation).

---

## Data Directories

Nymeria uses the following directories under `settings.data_dir`. In source
checkouts that defaults to `Nymeria/data/`; in packaged installs it defaults to
`~/.nymeria/data/`. `nymeria init` creates this layout, writes
`~/.nymeria/config.env`, and initializes the first admin token.

| Directory | Purpose |
|-----------|---------|
| `data/nymeria.db` | SQLite conversation database |
| `data/accounts.db` | Account users, tokens, and chat-app bindings |
| `data/todo_schedule.db` | SQLite scheduled TODO index for polling |
| `data/BOOTSTRAP_TOKEN.txt` | First-run admin bootstrap token, written with mode 0600 |
| `data/todos/` | TODO list storage (`{user_id}.json`) |
| `data/logs/` | HTTP/API primitive tool audit logs (`audit_YYYYMMDD.jsonl`) |
| `data/users/` | User profiles, memories, thread configs, activity logs, triggers |
| `data/backups/` | Self-modification backups |
| `data/snapshots/` | User-data snapshot artifacts (`snapshot-<utc>.nysnap`); see `docs/deployment/backup-and-restore.md` |
| `data/custom_tools/` | Custom tool definitions (`{tool_id}.json`) |
| `data/mcp_servers/` | MCP server configuration storage |
| `data/notifications/` | User notification storage |

These directories and files are created automatically on first run.

---

## System Prompt (soul.md)

The packaged default lives at `nymeria/config/soul.md` in the source tree or
installed package. This file defines Nymeria's:
- Personality and tone
- Capabilities and limitations
- Guidelines for tool usage
- Autonomous behavior rules

### Editing the base system prompt

There are two ways to customize the base prompt for the whole instance:

- **In-app (recommended):** Settings -> System Prompt (admin only). Saving writes
  a data-dir override at `data/system_prompt.md` and hot-reloads the agent, so
  the new persona applies to new turns immediately without a restart. "Reset to
  default" deletes the override and restores the packaged `soul.md`. The
  git-tracked `soul.md` is never modified, and the override works on read-only
  packaged installs.
- **On disk:** edit `nymeria/config/soul.md` directly (source checkouts only;
  changes take effect on agent restart).

Precedence: `load_soul()` returns the `data/system_prompt.md` override when it is
present and non-empty, otherwise the packaged `soul.md`. The same editor is
exposed over the admin-only `GET/PUT/DELETE /settings/system-prompt` endpoints (a
blank `PUT` body clears the override).

Per-thread overlays still apply on top of the base prompt: a thread's custom
instructions are appended, and a thread's system-prompt override replaces the
base for that thread only (Thread Settings -> System Prompt). A thread's
free-form notepad (its persistent memory) is editable at Thread Settings ->
Notepad, backed by `GET/PUT /threads/{id}/notepad`.

---

## Example .env

```bash
# LLM Configuration
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-6
LLM_TEMPERATURE=1.0
ANTHROPIC_API_KEY=sk-ant-...

# Advanced LLM Settings (all optional)
# LLM_BASE_URL=                       # Override API endpoint (e.g., local proxy)
# OPENAI_API_MODE=responses           # OpenAI-compatible mode: responses or chat_completions
# LLM_STREAM_MAX_RETRIES=2            # Retry transient failures, rewinding to checkpoints after partial streams
# LLM_STREAM_RETRY_INITIAL_DELAY=1.0
# LLM_STREAM_RETRY_MAX_DELAY=8.0
# LLM_MAX_TOKENS=4096
# LLM_TOP_P=0.95
# LLM_TOP_K=40
# LLM_FREQUENCY_PENALTY=0.0
# LLM_PRESENCE_PENALTY=0.0
# LLM_REASONING_EFFORT=medium  # off|low|medium|high|xhigh|max (clamped per model)
# LLM_USE_MODEL_DEFAULTS=false # Let provider use model-specific optimal defaults
# MEMORY_CHAR_LIMIT=8000       # Global/default persisted memory character budget
# MEMORY_MAX_ENTRIES=100       # Max global key-value memories per user
# MEMORY_VALUE_MAX_CHARS=1000  # Max characters per global memory value
# AGENT_MAX_ITERATIONS=500     # Agent loop safety backstop per turn

# Web search (optional but recommended)
PERPLEXITY_API_KEY=pplx-...

# Database (SQLite is default - no additional config needed)
DATABASE_BACKEND=sqlite
# SQLITE_PATH=/path/to/nymeria.db  # Uncomment to customize path

# API Server
# Authentication uses per-user account tokens; the bootstrap admin token is
# written to <data_dir>/BOOTSTRAP_TOKEN.txt on first boot. See docs/accounts.md.
# NYMERIA_SERVICE_TOKEN is the admin service token used by bots and the worker
# ticker (with X-Nymeria-Act-As) for per-user routing.
NYMERIA_SERVICE_TOKEN=nym_<admin-service-token>
# Public browser URL for one-time credential setup links in chat bots.
# Localhost HTTP is fine for local development; production should use HTTPS.
# NYMERIA_PUBLIC_URL=https://nymeria.example.com
# Destination for the desktop "Report problem" button. Use a dedicated inbox,
# not a personal one. Sent via the Outlook email tool. Unset = /report 503s.
# NYMERIA_ERROR_REPORT_EMAIL=reports@example.com
API_HOST=0.0.0.0
API_PORT=8000
# NYMERIA_API_DOCS=false            # Set true only in trusted local development
# NYMERIA_DEBUG=false               # Also enables API docs when true
# NYMERIA_WORKSPACE_DIR=/workspace
# NYMERIA_CONFINE_FILE_TO_WORKSPACE=false
# NYMERIA_ALLOW_SELF_EDIT=true
# NYMERIA_ALLOW_UNSANDBOXED_MCP_INSTALL=true
# NYMERIA_ENFORCE_MCP_STDIO_ALLOWLIST=false

# Logging
LOG_LEVEL=INFO
AUDIT_LOG_ENABLED=true

# HTTP tool egress policy (optional - defaults shown)
# HTTP_INTERNAL_ALLOWLIST=          # e.g. host.docker.internal:1420
# HTTP_DOMAIN_ALLOWLIST=
# HTTP_DOMAIN_BLOCKLIST=
# HTTP_MAX_REDIRECTS=5
# HTTP_ALLOW_HTTPS_TO_HTTP_REDIRECT=false

# Autonomous Operation (optional - defaults shown)
# TICKER_POLL_INTERVAL=5
# MAX_CONCURRENT_AUTONOMOUS=5
# MAX_CONCURRENT_INTERACTIVE=0          # 0 = unlimited (interactive admission control off)
# INTERACTIVE_ADMISSION_WAIT_SECONDS=10 # bounded wait before shedding with 429

# Context Management (optional - defaults shown)
# CONTEXT_MANAGEMENT=auto_compact      # auto_compact, sliding_window, or none
# COMPACT_THRESHOLD_MODE=tokens        # tokens (default) | percentage
# COMPACT_THRESHOLD=0.8                # When mode=percentage: trigger at this fraction of context limit (0.05-0.95)
# COMPACT_THRESHOLD_TOKENS=200000      # When mode=tokens: absolute input-token trigger (1000-2000000)
# COMPACT_MODEL=                       # Use cheaper model for summarization
# SLIDING_WINDOW_CYCLES=5              # For legacy sliding_window mode

# Tool output safety
# TOOL_OUTPUT_MAX_CHARS=100000     # Max stored characters per tool result
# TOOL_TIMING_IN_RESULTS=false     # Append server-measured [Duration: ...] to tool results the model sees
# BASH_ENV_PASSTHROUGH=            # Extra env var names for bash_execute (comma-separated; base allowlist is PATH,HOME,LANG,LC_ALL,TMPDIR)

# Activity Log (optional)
# ACTIVITY_RETENTION_HOURS=12      # Hours to retain activity log entries

# Messaging Platforms (optional - for notify tool)
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_DEFAULT_CHAT_ID=
# DISCORD_WEBHOOK_URL=
# DISCORD_BOT_TOKEN=
# DISCORD_MODE=gateway             # gateway or webhook
# SLACK_WEBHOOK_URL=

# CORS (optional - for remote frontends)
# CORS_ORIGINS=http://localhost:1420,tauri://localhost,http://tauri.localhost,https://tauri.localhost,http://localhost:8000
# Add exact LAN/production frontend origins as needed. Wildcard origins are rejected.
```

---

## Provider-Specific Configuration

### Anthropic (Recommended)

```bash
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-6
ANTHROPIC_API_KEY=sk-ant-...
```

Available models:
- `claude-opus-4-7` (current flagship; the provider already handles the thinking/sampling-param caveats for this model)
- `claude-opus-4-6` (previous flagship)
- `claude-sonnet-4-6` (balanced default)
- `claude-opus-4-20250514`
- `claude-haiku-3-5-20241022` (fastest)

### OpenAI

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-5.5
OPENAI_API_KEY=sk-...
EMBEDDING_API_KEY=sk-...   # Optional; used by memory/skill semantic search
```

### OpenRouter

```bash
LLM_PROVIDER=openrouter
LLM_MODEL=anthropic/claude-sonnet-4-6
OPENROUTER_API_KEY=sk-or-...
```

OpenRouter provides access to many models from different providers through a unified API.

Nymeria defaults OpenRouter to OpenRouter's beta Responses API (`/api/v1/responses`) with `store=false`. OpenRouter's Responses API is stateless, so Nymeria sends the full checkpointed conversation history on each request instead of using `previous_response_id`. To use the older `/chat/completions` endpoint, set `OPENAI_API_MODE=chat_completions` globally or choose **Chat Completions** in the global/per-thread API Mode selector. Errors from the beta Responses endpoint are surfaced directly so the mode choice stays explicit.

**Tested Compatible Models:**
- `anthropic/claude-sonnet-4-6` - Recommended
- `anthropic/claude-sonnet-4.5` - Previous recommended
- `minimax/minimax-m2.1` - Fast responses
- `deepseek/deepseek-v3.2` - Good performance
- `google/gemini-3-pro-preview` - Functional
- `z-ai/glm-4.7` - Basic compatibility

Most OpenRouter models work with Nymeria's agent harness, including tool calling.

### Local Proxy (e.g., CLIProxyAPI)

Route requests through your local CLIProxy deployment to use subscription
OAuth where supported instead of per-API-call billing. This is an advanced path:
follow your CLIProxy setup notes, keep the pinned image and Nymeria proxy
headers/fingerprint behavior unchanged, and run the documented smoke tests
before routing real traffic.

First-run CLIProxy OAuth setup through `nymeria init` is deferred and is not part
of the rebuilt wizard in this phase; use a direct provider API key for first-run,
or configure CLIProxy manually. From an already-connected admin desktop session, use
Settings > Provider > Open Wizard to point the backend at an already-running
proxy endpoint; installed desktop builds do not start CLIProxy or perform OAuth
login.

CLIProxy and OpenAI-compatible providers configure via standard base-URL/API-key settings; see the provider-specific docs.

**Provider-aware base URL**: When `LLM_BASE_URL` is set globally, it applies to all threads using the global provider. Threads with a per-thread provider override to a *different* provider (e.g., `openrouter`) ignore the global base URL and use the provider's standard endpoint. This allows callable threads to route through OpenRouter while the main thread uses the proxy.

The global LLM settings are intentionally not account-scoped. Two users on the same Nymeria server cannot have different "global" providers; the last admin save wins for the deployment. To give one user's thread a different provider, configure that thread's LLM override instead.

---

## Database Backends

### SQLite (Default)

No additional configuration needed. The database is created at
`<data_dir>/nymeria.db`, which is `Nymeria/data/nymeria.db` in a source
checkout and `~/.nymeria/data/nymeria.db` in a packaged install unless
`NYMERIA_DATA_DIR` or `SQLITE_PATH` overrides it.

```bash
DATABASE_BACKEND=sqlite
# Optional: customize path
# SQLITE_PATH=/path/to/nymeria.db
```

### PostgreSQL

For production deployments with multiple instances:

```bash
DATABASE_BACKEND=postgres
POSTGRES_URI=postgresql://user:password@localhost:5432/nymeria
```

Docker installs the PostgreSQL checkpoint dependencies through
`requirements-docker.txt`, which includes `requirements-postgres.txt` rather
than duplicating those package entries. Docker images install runtime
requirements only; backend tests, coverage, and linting use
`requirements-dev.txt` so `pytest`, `pytest-cov`, and `ruff` do not ship in
production images. For local development, install the relevant files
explicitly after the base requirements:

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
pip install -r requirements-postgres.txt
```

The default SQLite checkpoint and local vector-search dependencies live in
`requirements-sqlite.txt`, and `requirements.txt` includes that file so the
zero-setup local backend remains the default install path.

### Memory (Testing)

State is lost on restart. Use for testing only:

```bash
DATABASE_BACKEND=memory
```
