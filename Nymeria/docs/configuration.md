# Nymeria Configuration

All configuration is done via environment variables. Source checkouts load
`.env`, `config.env`, and `.env.docker` from the backend root if present;
when the same variable appears in multiple files, `.env.docker` has the
highest dotenv-file precedence. Packaged `nymeria` installs use
`~/.nymeria/config.env` by default. Copy `.env.docker.example` to
`.env.docker` for the full local/Docker template, run `nymeria init` for
packaged setup, or create `.env` manually for a lighter source-checkout setup.

**Note:** Nymeria validates configuration on startup. If required keys are missing, you'll see clear error messages with instructions.

## Environment Variables

### LLM Configuration

These variables are deployment-wide server defaults, not per-user account preferences. In a multi-user deployment, every thread that does not set a per-thread LLM override inherits the same `LLM_PROVIDER`, `LLM_MODEL`, API key, and `LLM_BASE_URL`; if an admin changes them through Settings → LLM, the change affects all users on that server. User-specific routing is currently done with per-thread overrides.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LLM_PROVIDER` | Yes | `anthropic` | LLM provider ID. Native Anthropic uses `anthropic`; OpenAI-compatible IDs include `openai`, `openrouter`, `xai`, `google`, `groq`, `deepseek`, `mistral`, local providers such as `ollama`/`lmstudio`, and the full registry documented in [`chat_completions_providers.md`](chat_completions_providers.md). |
| `LLM_MODEL` | Yes | `claude-sonnet-4-6` | Model identifier for the provider |
| `LLM_FAST_MODEL` | No | provider-aware | Fast model used by CLI `/fast`; when unset, `/fast` picks a provider-aware default |
| `LLM_FALLBACK_MODELS` | No | `anthropic:claude-haiku-4-5-20251001` | Comma-separated ordered fallback models tried by the backend when the primary model fails with a transient provider/transport error before output starts. Entries use the active provider by default, or `provider:model-id` for any known provider in the LLM registry. CLI shortcut: `/fallback`. |
| `LLM_TEMPERATURE` | No | `1.0` | Sampling temperature (0.0 - 2.0) |

### Advanced LLM Settings (Optional)

These settings give power users fine-grained control over LLM behavior. All are optional and only sent to the API if explicitly set.

| Variable | Default | Range | Description |
|----------|---------|-------|-------------|
| `LLM_MAX_TOKENS` | (model limit) | 1 - 1,000,000 | Maximum output tokens |
| `LLM_TOP_P` | (provider default) | 0.0 - 1.0 | Nucleus sampling threshold |
| `LLM_TOP_K` | (provider default) | 1 - 100 | Top-k sampling (limits vocabulary per step) |
| `LLM_FREQUENCY_PENALTY` | (provider default) | -2.0 - 2.0 | Reduce repetition of token sequences |
| `LLM_PRESENCE_PENALTY` | (provider default) | -2.0 - 2.0 | Encourage new topics |
| `LLM_REASONING_EFFORT` | (none) | low/medium/high | For reasoning models (o1, Claude with thinking); invalid values fail settings validation |
| `LLM_EXTENDED_THINKING` | `false` | true/false | Enable extended thinking/reasoning for compatible models. CLI shortcut: `/reasoning on\|off\|low\|medium\|high` (alias `/thinking`) sets both fields in one command. `/fast` toggles the active thread between `LLM_MODEL` and `LLM_FAST_MODEL`; `/fast <prompt>` uses the fast model for that turn only. |
| `LLM_USE_MODEL_DEFAULTS` | `false` | true/false | Use model-specific defaults for temperature, top_p, and frequency penalty instead of global values. When enabled, these params are not sent to the API — the provider applies the model's own optimal defaults. |
| `LLM_BASE_URL` | (provider default) | URL | Override API endpoint for native Anthropic or OpenAI-compatible providers. For `anthropic` CLIProxy, use the root URL with no `/v1` suffix because `ChatAnthropic` appends `/v1/messages`; for OpenAI-compatible endpoints, use the provider's documented base URL, usually ending in `/v1`. Leave unset to use the registry default or a credential-vault base URL. |
| `OPENAI_API_MODE` | `responses` | responses/chat_completions | API mode for OpenAI-compatible providers. `responses` is used only for providers that advertise Responses support in the registry; unsupported providers fall back to Chat Completions. |
| `LLM_STREAM_MAX_RETRIES` | `2` | 0 - 10 | Retries for transient LLM call/stream failures. Streaming retries only happen before any model chunk is emitted. |
| `LLM_STREAM_RETRY_INITIAL_DELAY` | `1.0` | 0 - 60 | Initial retry backoff delay in seconds |
| `LLM_STREAM_RETRY_MAX_DELAY` | `8.0` | 0 - 300 | Maximum retry backoff delay in seconds |

**Note:** For model dropdowns and context metadata, Nymeria asks the selected provider's `/models` endpoint through `GET /models/available`. Provider-returned context fields such as `context_length`, `context_window`, or `max_context_tokens` are cached for frontend context-window percentage calculations. Chat Completions itself standardizes usage token fields, not context-window limits.

**Also note:** `LLM_EXTENDED_THINKING`, `LLM_USE_MODEL_DEFAULTS`, `OPENAI_API_MODE`, and provider-aware `LLM_BASE_URL` overrides are implemented in settings and runtime behavior, so they are safe to rely on even though some older docs may mention proxy behavior separately. OpenRouter Responses reasoning is displayed only when the provider emits plaintext reasoning fields; malformed inline `<think>` text that arrives as normal answer text is stripped from display and replay.

`LLM_FALLBACK_MODELS` is backend-owned, so it applies to every chat surface:
desktop, mobile, CLI, bots, triggers, scheduled TODOs, and callable-thread
invocations. Fallbacks are only attempted for retryable failures such as 429s,
5xx responses, timeouts, or transport errors before the model has emitted any
response chunks; after streaming starts, Nymeria does not switch models because
that would duplicate visible output.

### API Keys

Set the API key for your chosen provider:

| Variable | Provider | Required |
|----------|----------|----------|
| `ANTHROPIC_API_KEY` | Anthropic | If using `anthropic` provider |
| `ANTHROPIC_DIRECT_API_KEY` | Anthropic | Optional direct Anthropic `sk-ant-*` key. Used only when the effective Anthropic base URL is empty/direct; CLIProxy Anthropic calls continue using `ANTHROPIC_API_KEY` (`cpx-*`). |
| `OPENAI_API_KEY` | OpenAI | If using `openai` provider; optional otherwise for OpenAI image generation, STT, and OpenAI-backed tools |
| `GEMINI_API_KEY` | Google Gemini | Optional; enables Gemini image generation, Gemini TTS, and Gemini attachment extraction |
| `OPENROUTER_API_KEY` | OpenRouter | If using `openrouter` provider |
| `EMBEDDING_API_KEY` | OpenAI-compatible embeddings | Optional; enables semantic memory/skill search. Keep separate from CLIProxy `OPENAI_API_KEY` values. |
| `EMBEDDING_BASE_URL` | OpenAI-compatible embeddings | Optional custom `/v1` base URL for embeddings |
| `EMBEDDING_MODEL` | OpenAI-compatible embeddings | Optional; defaults to `text-embedding-3-small`; must return 1536-dimensional vectors |
| `PERPLEXITY_API_KEY` | Perplexity | Required for `web_search` tool |
| `WOLFRAM_ALPHA_APP_ID` | Wolfram\|Alpha | Optional env fallback for `wolfram_alpha_query`; credential vault provider `wolfram_alpha` is preferred |
| `SEARXNG_BASE_URL` | SearXNG | Optional env fallback for `searxng_search`; credential vault provider `searxng` is preferred |
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
| `TELEGRAM_API_BASE_URL` | Telegram | Optional Telegram Bot API base URL override |
| `WEBEX_ACCESS_TOKEN` | Webex | Optional Webex access token fallback; credential vault provider `webex` is preferred |
| `WEBEX_BASE_URL` | Webex | Optional Webex API base URL override |
| `WHATSAPP_ACCESS_TOKEN` | WhatsApp Business Cloud | Optional WhatsApp access token fallback; credential vault provider `whatsapp` is preferred |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | WhatsApp Business Cloud | Optional WhatsApp business account ID fallback |
| `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp Business Cloud | Optional WhatsApp sender phone number ID fallback |
| `WHATSAPP_BASE_URL` | WhatsApp Business Cloud | Optional WhatsApp Graph API base URL override |
| `DISCORD_BASE_URL` | Discord | Optional Discord REST API base URL override |
| `MATTERMOST_ACCESS_TOKEN` | Mattermost | Optional Mattermost access token fallback; credential vault provider `mattermost` is preferred |
| `MATTERMOST_BASE_URL` | Mattermost | Mattermost server base URL for native tools |
| `MATRIX_ACCESS_TOKEN` | Matrix | Optional Matrix access token fallback; credential vault provider `matrix` is preferred |
| `MATRIX_BASE_URL` | Matrix | Optional Matrix homeserver or client API base URL override |
| `ROCKETCHAT_AUTH_TOKEN` | Rocket.Chat | Optional Rocket.Chat auth token fallback; credential vault provider `rocketchat` is preferred |
| `ROCKETCHAT_USER_ID` | Rocket.Chat | Optional Rocket.Chat user ID fallback |
| `ROCKETCHAT_BASE_URL` | Rocket.Chat | Rocket.Chat server base URL for native tools |
| `ZULIP_API_KEY` | Zulip | Optional Zulip API key fallback; credential vault provider `zulip` is preferred |
| `ZULIP_EMAIL` | Zulip | Optional Zulip bot/user email fallback |
| `ZULIP_BASE_URL` | Zulip | Zulip organization base URL for native tools |
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

`nymeria init` can collect these optional capability keys during first-run
setup after the user chooses a hosting/security profile. For bare-metal and
venv/pipx hosting, it validates the selected provider/model/key combination
with a small LLM API call before writing `config.env`. Interactive setup offers
a provider-specific default model and accepts Enter to use it. The venv/pipx
profile isolates Python packages only; it is not an OS security sandbox, and
Nymeria can still access files your user can access when tools are enabled. In
non-interactive mode, pass `--embedding-api-key`, `--openai-api-key`,
`--gemini-api-key`, or `--perplexity-api-key`; keys that are not supplied are
left out of `config.env`. Use `--data-dir` with `--setup-style advanced` when
`NYMERIA_DATA_DIR` should differ from the runtime root's `data/` directory.
Non-interactive setup defaults to direct API-key auth and printed next commands.
It accepts `--hosting venv|bare_metal`, `--auth-method api_key`,
`--setup-style advanced|recommended`, and
`--next-action print_commands|cli|start_api_open_frontend`. `--hosting docker`
with direct API-key setup prints source-checkout Docker setup commands and
exits without writing `config.env` or `.env.docker`, so Docker credentials are
not silently written to the wrong runtime root. CLIProxy Claude and
Codex/OpenAI OAuth auth methods are advanced local setup paths that use the
pinned `CLIProxyAPI-main/temp/latest/` deployment, require active local OAuth
auth files, write only a local `cpx-*` gatekeeper key into Nymeria config, and
stop before writing config if their verification probes fail. Codex/OpenAI
setup writes `LLM_PROVIDER=openai`,
`OPENAI_API_MODE=responses`, `OPENAI_API_KEY=<cpx-gatekeeper-key>`, and an
`LLM_BASE_URL` ending in `/v1`; do not use that `cpx-*` value for
`EMBEDDING_API_KEY`. Add `--skip-llm-test` only for deliberate
offline/scripted direct API-key setup where provider access will be verified
separately.

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_BACKEND` | `sqlite` | Backend type: `sqlite`, `postgres`, or `memory` |
| `SQLITE_PATH` | `<data_dir>/nymeria.db` | SQLite database file location. Relative custom paths resolve from the runtime project root |
| `POSTGRES_URI` | - | PostgreSQL connection string (if using postgres) |
| `USER_TIMEZONE` | `UTC` | IANA timezone used for time context and absolute schedule parsing. Docker also mirrors this into `TZ` so OS-level time output stays aligned. |

### API Server and Paths

| Variable | Default | Description |
|----------|---------|-------------|
| `NYMERIA_API_KEY` | - | **Deprecated / ignored.** Formerly a shared bearer token; authentication now uses per-user account tokens. Safe to delete from `.env.docker`. See `docs/accounts.md`. |
| `NYMERIA_SERVICE_TOKEN` | mode-required | Admin-role Nymeria account token used by bots, ticker, watchdog, trigger-fires, slash commands, and the public MCP thin client for X-Nymeria-Act-As calls. `run.py` fails fast without it for `worker`, `discord-bot`, `telegram-bot`, `twitch-bot`, `watchdog`, `mcp`, and `service run`; local `api`, `cli`, and `users` development can still start without it. Created via `python run.py users add --role admin`. See `docs/accounts.md`. |
| `NYMERIA_API_URL` | auto | Local API URL for thin clients and in-process tools (MCP server, `slash_command`). Defaults to Docker service URLs when applicable, otherwise `http://localhost:8000` |
| `API_HOST` | `0.0.0.0` | Server bind address |
| `API_PORT` | `8000` | Server port |
| `NYMERIA_API_DOCS` | `false` | Expose FastAPI Swagger UI, ReDoc, and `/openapi.json`. Disabled by default for beta deployments; changing it requires an API restart |
| `NYMERIA_DEBUG` | `false` | Enables debug-only server behavior, including API docs/schema routes. Use only in trusted local development |
| `CORS_ORIGINS` | `http://localhost:1420,tauri://localhost,http://tauri.localhost,https://tauri.localhost,http://localhost:8000` | Comma-separated allowed CORS origins. Wildcard origins are rejected because credentialed CORS is enabled |
| `NYMERIA_DATA_DIR` | `<project_root>/data` | Override data directory path. For pipx/wheel installs, the project root defaults to `~/.nymeria`, so the effective default is `~/.nymeria/data` |
| `NYMERIA_PROJECT_ROOT` | auto-detected | Override runtime project root resolution. Source launches use the checkout's `Nymeria/` root; packaged/frozen launches default to `~/.nymeria` |

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
default, or the custom `--data-dir` value in advanced setup, so a later root or
data-directory move should update that value or rerun `nymeria init --root ...`
or `nymeria init --setup-style advanced --data-dir ...`.
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

The CLI exposes the LLM provider flow through `/provider`. `/provider set
<provider> api_key=<key>` stores a local copy in `~/.nymeria/credentials.json`
with private file permissions, then applies the mapped write-only backend
setting when the active API token has admin access. `/provider test [provider]`
uses the local credential, or an admin-only unmasked backend env lookup when no
local key is stored, and calls the same `/settings/llm/test` probe. `/provider
switch <provider>` patches `llm_provider` and reapplies any locally stored
credential for that provider.

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
| `BROWSER_VERIFY_SSL` | `true` | Verify TLS certificates for fallback HTTP requests. Set to `false` only for trusted environments with known TLS interception |

### Redis (Docker Only)

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_ENABLED` | `false` | Enable Redis event bus for cross-container communication |
| `REDIS_PASSWORD` | required in Docker | Redis password used by the Compose Redis service and the generated `REDIS_URL`. Use a URL-safe value such as `openssl rand -hex 32`. |
| `REDIS_URL` | - | Redis connection URL. Docker Compose generates `redis://:<REDIS_PASSWORD>@redis:6379/0`; local non-Docker development can use `redis://localhost:6379` if Redis auth is disabled. Startup logs redact credentials from this URL. |

### Messaging Platforms

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | - | Telegram bot token from @BotFather |
| `TELEGRAM_API_BASE_URL` | `https://api.telegram.org` | Telegram Bot API base URL for native Telegram tools |
| `TELEGRAM_DEFAULT_CHAT_ID` | - | Default Telegram chat ID for notifications |
| `WEBEX_ACCESS_TOKEN` | - | Webex access token fallback for native Webex tools |
| `WEBEX_BASE_URL` | `https://webexapis.com/v1` | Webex API base URL |
| `WHATSAPP_ACCESS_TOKEN` | - | WhatsApp Business Cloud access token fallback |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | - | WhatsApp business account ID fallback |
| `WHATSAPP_PHONE_NUMBER_ID` | - | WhatsApp sender phone number ID fallback |
| `WHATSAPP_BASE_URL` | `https://graph.facebook.com/v19.0` | WhatsApp Graph API base URL |
| `DISCORD_WEBHOOK_URL` | - | Discord webhook URL for notifications |
| `DISCORD_BOT_TOKEN` | - | Discord bot token for two-way communication |
| `DISCORD_MODE` | `gateway` | Discord connection mode: `gateway` or `webhook` |
| `DISCORD_RESPOND_MODE` | `mention` | Guild behavior: `mention` (only @Nymeria) or `all` |
| `SLACK_WEBHOOK_URL` | - | Slack webhook URL for notifications |
| `SLACK_BOT_TOKEN` | - | Slack bot token for two-way communication |
| `TEAMS_TEAM_ID` | - | Microsoft Teams team ID for notifications |
| `TEAMS_CHANNEL_ID` | - | Microsoft Teams channel ID for notifications |
| `TEAMS_ACCOUNT_ID` | - | Outlook account ID for Teams (must have ChannelMessage.Send) |
| `OUTLOOK_DEFAULT_ACCOUNT_ID` | - | Default Outlook account for email tools |
| `MICROSOFT_MCP_CLIENT_ID` | `8ad36cab...` | Azure AD app client ID for Outlook/Teams OAuth |
| `GOOGLE_OAUTH_CREDENTIALS` | - | Path to Google OAuth credentials JSON file |
| `_PRV_A_SERVICE_ACCOUNT_FILE` | - | Path to a Google service account JSON file for _PRV_A reference Sheets |
| `PERPLEXITY_API_KEY` | - | Perplexity API key for web_search tool |
| `WOLFRAM_ALPHA_APP_ID` | - | Wolfram\|Alpha AppID for wolfram_alpha_query |
| `SEARXNG_BASE_URL` | - | Base URL for a SearXNG instance used by searxng_search |
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
| `SLACK_BOT_TOKEN` | - | Slack bot token fallback |
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
| `TELEGRAM_API_BASE_URL` | `https://api.telegram.org` | Telegram Bot API base URL |
| `WEBEX_ACCESS_TOKEN` | - | Webex access token fallback |
| `WEBEX_BASE_URL` | `https://webexapis.com/v1` | Webex API base URL |
| `WHATSAPP_ACCESS_TOKEN` | - | WhatsApp Business Cloud access token fallback |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | - | WhatsApp business account ID fallback |
| `WHATSAPP_PHONE_NUMBER_ID` | - | WhatsApp sender phone number ID fallback |
| `WHATSAPP_BASE_URL` | `https://graph.facebook.com/v19.0` | WhatsApp Graph API base URL |
| `DISCORD_BASE_URL` | `https://discord.com/api/v10` | Discord REST API base URL |
| `MATTERMOST_ACCESS_TOKEN` | - | Mattermost access token fallback |
| `MATTERMOST_BASE_URL` | - | Mattermost server base URL |
| `MATRIX_ACCESS_TOKEN` | - | Matrix access token fallback |
| `MATRIX_BASE_URL` | `https://matrix-client.matrix.org/_matrix/client/v3` | Matrix Client-Server API base URL |
| `ROCKETCHAT_AUTH_TOKEN` | - | Rocket.Chat auth token fallback |
| `ROCKETCHAT_USER_ID` | - | Rocket.Chat user ID fallback |
| `ROCKETCHAT_BASE_URL` | - | Rocket.Chat server base URL |
| `ZULIP_API_KEY` | - | Zulip API key fallback |
| `ZULIP_EMAIL` | - | Zulip bot/user email fallback |
| `ZULIP_BASE_URL` | - | Zulip organization base URL |
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
| `TWITCH_CLIENT_ID` | - | Twitch application Client ID |
| `TWITCH_CLIENT_SECRET` | - | Twitch application Client Secret |
| `TWITCH_BOT_ACCESS_TOKEN` | - | Bot's OAuth access token |
| `TWITCH_BOT_REFRESH_TOKEN` | - | Bot's OAuth refresh token |
| `TWITCH_BOT_USER_ID` | - | Bot's numeric Twitch user ID |
| `TWITCH_BROADCASTER_TOKEN` | - | Broadcaster's OAuth token (channel:bot scope) |
| `TWITCH_BROADCASTER_REFRESH_TOKEN` | - | Broadcaster's refresh token |
| `TWITCH_CHANNEL` | - | Twitch channel to join (required when `twitch-bot` runs) |
| `TWITCH_SYSTEM_PROMPT` | - | Optional initial system prompt for the Twitch thread; existing thread config takes precedence |
| `TWITCH_BUFFER_SIZE` | `500` | Chat message ring buffer size (50-5000) |
| `TWITCH_PULSE_ENABLED` | `true` | Enable periodic chat evaluation |
| `TWITCH_PULSE_INTERVAL` | `300` | Seconds between pulse checks (60-3600) |
| `TWITCH_PULSE_MIN_MESSAGES` | `10` | Minimum new messages before a pulse fires |
| `TWITCH_PULSE_MESSAGE_COUNT` | `100` | Messages to include in pulse context |
| `TWITCH_COMMAND_CONTEXT_COUNT` | `50` | Messages to include with !ask context |
| `TWITCH_RESPOND_MODE` | `command` | Response mode (command = only !commands) |

### Log File Rotation

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVICE_LOG_FILE` | `service.log` | Rotating log filename (all modes) |
| `SERVICE_LOG_MAX_BYTES` | `10485760` | Rotate log after this many bytes |
| `SERVICE_LOG_BACKUP_COUNT` | `5` | Number of rotated log backups to keep |
| `LOG_LEVEL` | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `AUDIT_LOG_ENABLED` | `true` | Log redacted HTTP/API primitive tool events to audit log |

### HTTP Tool Egress Policy

These settings apply to `http_request`, `api_discover`, and saved custom HTTP
tools. The model cannot override them per call.

| Variable | Default | Description |
|----------|---------|-------------|
| `HTTP_INTERNAL_ALLOWLIST` | - | Comma-separated exact internal hosts or `host:port` pairs that HTTP tools may reach, e.g. `host.docker.internal:1420,homeassistant.local:8123` |
| `HTTP_DOMAIN_ALLOWLIST` | - | Optional comma-separated public-domain allowlist. Supports exact domains and `*.example.com` wildcards. Empty means public domains are allowed unless blocked. |
| `HTTP_DOMAIN_BLOCKLIST` | - | Comma-separated public-domain blocklist. Supports exact domains and `*.example.com` wildcards. |
| `HTTP_MAX_REDIRECTS` | `5` | Maximum redirects followed by HTTP tools (0-20) |
| `HTTP_ALLOW_HTTPS_TO_HTTP_REDIRECT` | `false` | Whether HTTP tools may follow redirects from `https://` to `http://` |

By default HTTP tools are public-internet-only. Loopback, private, link-local,
reserved, unspecified, multicast, and metadata targets are blocked after DNS
resolution unless the target is a non-metadata host explicitly listed in
`HTTP_INTERNAL_ALLOWLIST`.

### Autonomous Operation

| Variable | Default | Description |
|----------|---------|-------------|
| `TICKER_POLL_INTERVAL` | `5` | Seconds between polls for due tasks (1-60) |
| `MAX_CONCURRENT_AUTONOMOUS` | `5` | Max concurrent autonomous tasks (`0` = unlimited) |
| `LOCK_TIMEOUT` | `120` | Seconds to wait on per-thread lock before timing out |
| `TOOL_TIMEOUT` | `300` | Max seconds a tool/sub-agent invocation may run |
| `TOOL_OUTPUT_MAX_CHARS` | `100000` | Max characters stored for one tool result. Larger outputs keep the first ~75k and last ~25k characters with a truncation marker. |

### Context Management

Nymeria automatically manages conversation context to prevent overflow. The default `auto_compact` mode summarizes conversations when approaching the model's context limit.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_MANAGEMENT` | `auto_compact` | Strategy: `auto_compact`, `sliding_window`, or `none` |
| `COMPACT_THRESHOLD` | `0.8` | Trigger compaction at this fraction of the model context window (0.05-0.95). Example: `0.38` is about 400k tokens on GPT-5.5's 1.05M window. |
| `COMPACT_KEEP_MESSAGES` | `4` | Minimum messages before compaction is allowed |
| `COMPACT_MODEL` | (main model) | Optional cheaper model for summarization |
| `SLIDING_WINDOW_CYCLES` | `5` | Legacy: cycles to keep when using `sliding_window` mode |

**Context Management Modes:**

- **`auto_compact`** (default): When token usage reaches the threshold, Nymeria:
  1. Asks the agent to summarize the conversation (it already has full context)
  2. Agent saves important facts to persistent memory via `memory_add(scope="global", ...)`
  3. Clears the conversation and persists a visible compaction notice with the summary
  4. On async `/chat` streams, compacts before the next provider call when prior usage already crossed the trigger; post-turn async compaction emits `compacting`/`compacted` and streams the resumed assistant continuation. Sync/manual paths attach the summary to the next user message.

- **`sliding_window`**: Legacy mode that simply removes old messages, keeping the last N cycles

- **`none`**: No automatic context management (manual `/compact` still available)

### Watchdog, TODO, and Push Notifications

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCHDOG_ENABLED` | `true` | Enable watchdog to monitor TODO staleness |
| `WATCHDOG_INTERVAL_MINUTES` | `5` | Minutes between watchdog checks (1-60) |
| `TODO_STALENESS_MINUTES` | `20` | Minutes without update before TODO is stale (5-1440) |
| `TODO_AUTO_ARCHIVE_DAYS` | `7` | Days after completion before the ticker removes completed TODOs from the active TODO JSON list (1-30) |
| `ACTIVITY_RETENTION_HOURS` | `12` | Hours to retain activity log entries (1-168) |
| `FCM_ENABLED` | `false` | Enable Firebase Cloud Messaging push notifications |
| `FCM_CREDENTIALS_JSON` | - | Path to Firebase service account JSON |
| `NYMERIA_WATCHDOG_DISABLED` | - | Set to `1` / `true` / `yes` at runtime to mute the watchdog without restarting. See also the file flag below. |

**Wear OS Firebase client config:**

The Wear OS companion app intentionally tracks
`nymeria-watch/app/google-services.json`. That file is the Android client
configuration consumed by the Google Services Gradle plugin for Firebase Cloud
Messaging, and it contains Firebase project/app identifiers plus the
Firebase-provisioned Android API key. Firebase documents these app config
values as safe to include in client code or checked-in configuration when the
key is restricted to Firebase services. Do not add server credentials or
non-Firebase Google API keys to this file; use a separate restricted key for
non-Firebase APIs.

The private server-side FCM credential remains
`Nymeria/firebase-service-account.json`, which is managed separately with
git-crypt and wired into Docker via `FCM_CREDENTIALS_JSON`.

**Watchdog runtime kill switches** (disable without restart):

- **Env var**: `NYMERIA_WATCHDOG_DISABLED=1` (re-read on every poll cycle)
- **File flag**: `{data_dir}/flags/watchdog-off` — persistent across container restarts because `/data` is a Docker volume. Create it with `docker exec nymeria-watchdog touch /data/flags/watchdog-off`; remove with `rm` to re-enable.

In Docker deployments the watchdog runs in its own container (`nymeria-watchdog`, defined in `docker-compose.yml`). It's a thin client that calls the API over HTTP — no `NymeriaAgent` in the watchdog process. To disable it entirely, set `WATCHDOG_ENABLED=false` and restart, or simply don't start the service (`docker compose stop watchdog`). See `docs/architecture.md` §4.2 for details.

### Voice (TTS / STT)

| Variable | Default | Description |
|----------|---------|-------------|
| `TTS_PROVIDER` | `none` | TTS provider: `none`, `gemini`, `openai`, `qwen3` |
| `TTS_BASE_URL` | (per provider) | TTS API base URL. Not used for Gemini (uses SDK). Defaults: OpenAI=`https://api.openai.com/v1`, Qwen3=`http://localhost:8880/v1` |
| `TTS_API_KEY` | (falls back to `OPENAI_API_KEY`) | API key for OpenAI/Qwen3 TTS. Gemini uses `GEMINI_API_KEY` instead |
| `TTS_MODEL` | `tts-1-hd` | Model name. Gemini: `gemini-3.1-flash-tts-preview`, OpenAI: `tts-1` / `tts-1-hd` |
| `TTS_VOICE` | `nova` | Voice identifier. Gemini: `Kore`, `Puck`, `Charon`, `Algenib`, `Leda`, `Orus`, `Zephyr` (30 total). OpenAI: `alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer` |
| `TTS_OUTPUT_FORMAT` | `mp3` | Output format: mp3, wav, opus, aac. Gemini always outputs MP3 (converted from WAV server-side) |
| `TTS_SPEED` | `1.0` | Playback speed 0.25-4.0. Not applicable for Gemini |
| `STT_PROVIDER` | `none` | STT provider: `none`, `openai`, `faster-whisper` |
| `STT_BASE_URL` | (per provider) | STT API base URL |
| `STT_API_KEY` | (falls back to `OPENAI_API_KEY`) | API key for STT |
| `STT_MODEL` | `gpt-4o-mini-transcribe` | STT model name |
| `STT_LANGUAGE` | - | Language hint (ISO 639-1, e.g., `en`) |
| `VOICE_DEFAULT_THREAD_ID` | - | Default thread for voice/watch interactions (falls back to `watch-default`) |

**Gemini TTS** requires `GEMINI_API_KEY` (also used for document extraction). Supports 200+ inline audio tags for expressive speech — e.g., `[whispers]`, `[excitedly]`, `[sighs]`. See [Gemini TTS prompting guide](https://ai.google.dev/gemini-api/docs/speech-generation).

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
| `data/custom_tools/` | Custom tool definitions (`{tool_id}.json`) |
| `data/mcp_servers/` | MCP server configuration storage |
| `data/notifications/` | User notification storage |

These directories and files are created automatically on first run.

---

## System Prompt (soul.md)

Located at `nymeria/config/soul.md` in the source tree or installed package.
This file defines Nymeria's:
- Personality and tone
- Capabilities and limitations
- Guidelines for tool usage
- Autonomous behavior rules

For source checkouts, edit this file to customize how Nymeria responds.
Packaged installs load the bundled package copy; keep local personality edits in
source or a custom package build until user-editable packaged prompts are added.
Changes take effect on agent restart.

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
# LLM_STREAM_MAX_RETRIES=2            # Retry transient failures before chunks stream
# LLM_STREAM_RETRY_INITIAL_DELAY=1.0
# LLM_STREAM_RETRY_MAX_DELAY=8.0
# LLM_MAX_TOKENS=4096
# LLM_TOP_P=0.95
# LLM_TOP_K=40
# LLM_FREQUENCY_PENALTY=0.0
# LLM_PRESENCE_PENALTY=0.0
# LLM_REASONING_EFFORT=medium  # For reasoning models
# LLM_USE_MODEL_DEFAULTS=false # Let provider use model-specific optimal defaults

# Web search (optional but recommended)
PERPLEXITY_API_KEY=pplx-...

# Database (SQLite is default - no additional config needed)
DATABASE_BACKEND=sqlite
# SQLITE_PATH=/path/to/nymeria.db  # Uncomment to customize path

# API Server
# Authentication uses per-user account tokens; the bootstrap admin token is
# written to <data_dir>/BOOTSTRAP_TOKEN.txt on first boot. See docs/accounts.md.
# NYMERIA_SERVICE_TOKEN is the admin service token used by bots/ticker/watchdog
# (with X-Nymeria-Act-As) for per-user routing.
NYMERIA_SERVICE_TOKEN=nym_<admin-service-token>
API_HOST=0.0.0.0
API_PORT=8000
# NYMERIA_API_DOCS=false            # Set true only in trusted local development
# NYMERIA_DEBUG=false               # Also enables API docs when true

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

# Context Management (optional - defaults shown)
# CONTEXT_MANAGEMENT=auto_compact  # auto_compact, sliding_window, or none
# COMPACT_THRESHOLD=0.8            # Trigger at 80% of context limit (0.05-0.95)
# COMPACT_MODEL=                   # Use cheaper model for summarization
# SLIDING_WINDOW_CYCLES=5          # For legacy sliding_window mode

# Tool output safety
# TOOL_OUTPUT_MAX_CHARS=100000     # Max stored characters per tool result

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
- `claude-opus-4-7` (current flagship — see `docs/cliproxy.md` "Claude 4.7 compatibility" for the thinking/sampling-param caveats the provider already handles)
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

Route requests through the pinned local CLIProxy deployment to use subscription
OAuth where supported instead of per-API-call billing. This is an advanced path:
follow [cliproxy.md](./cliproxy.md), keep the pinned image and Nymeria proxy
headers/fingerprint behavior unchanged, and run the documented smoke tests
before routing real traffic.

For first-run setup from a source checkout, `nymeria init --auth-method
cliproxy_claude_oauth` and `nymeria init --auth-method
cliproxy_codex_oauth` can prepare the existing
`CLIProxyAPI-main/temp/latest/` deployment and write Nymeria config only after
verification succeeds. From an already-connected admin desktop session, use
Settings > Provider > Open Wizard to point the backend at an already-running
proxy endpoint; installed desktop builds do not start CLIProxy or perform OAuth
login.

Two runtime shapes are supported:

#### Native Anthropic (Recommended)

Uses `ChatAnthropic` with native `/v1/messages` format. No format translation — tool calling, streaming, and extended thinking work identically to direct API usage. Requires CLIProxyAPI with Claude OAuth login (`-claude-login`).

```bash
LLM_PROVIDER=anthropic
LLM_MODEL=claude-opus-4-7             # Must match a model in proxy's Claude registry
LLM_BASE_URL=http://localhost:8317    # No /v1 suffix — ChatAnthropic appends /v1/messages
ANTHROPIC_API_KEY=cpx-...             # Proxy gatekeeper key, not a hosted Anthropic key
```

When `LLM_BASE_URL` is set for the `anthropic` provider, `ChatAnthropic` is configured with:
- `anthropic_api_url` pointed at the proxy
- A `User-Agent: claude-cli/2.1.113` header that tells CLIProxyAPI v6.9.36 to skip system prompt cloaking (so Nymeria's own `soul.md` is preserved)
- Loop-local async HTTP clients so cached graph/model objects are safe when regular API chat and callable/autonomous bridge execution use different asyncio event loops

#### OpenAI-Compatible / Codex OAuth

Uses Nymeria's `ChatOpenAIWithReasoning` subclass pointed at the proxy's OpenAI-compatible endpoint. The base URL must include `/v1`; otherwise Responses mode posts to `/responses` and CLIProxy returns `404 page not found`.

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-5.5                      # Model name from the proxy's /v1/models
OPENAI_API_MODE=responses
OPENAI_API_KEY=cpx-latest-local-test   # Proxy gatekeeper key, not a hosted OpenAI key
LLM_BASE_URL=http://localhost:8317/v1  # Proxy endpoint (with /v1 suffix)
EMBEDDING_API_KEY=sk-...               # Optional hosted/local embeddings key; do not use cpx-* here
```

For GPT-5.5 through Codex OAuth, run the pinned CLIProxy deployment documented
in `docs/cliproxy.md`. To route individual threads, use **Thread Settings →
Model → OpenAI (Custom base URL)** and set the thread-level Base URL/API Key
fields; the default OpenAI API mode is `Responses API`, with `Chat Completions`
available only as a compatibility override and not recommended if thinking is
enabled. To route the whole deployment, use **Settings → Provider → Open
Wizard** or **Settings → LLM → OpenAI (Custom base URL)** and keep
`OPENAI_API_MODE=responses` in the active env file. Per-thread overrides honor
`provider`, `base_url`, `api_key`, and `openai_api_mode` — the API key is the
CLIProxy gatekeeper key (e.g. `cpx-latest-local-test`), not an upstream OpenAI
key.

OpenAI-compatible providers also receive Nymeria-managed loop-local `http_async_client` pools. This bypasses LangChain's process-global async `httpx` client cache so direct OpenAI, OpenRouter, and CLIProxy/Codex models remain safe when a cached thread graph is used from both the FastAPI event loop and the sync stream-bridge loop.

**Provider-aware base URL**: When `LLM_BASE_URL` is set globally, it applies to all threads using the global provider. Threads with a per-thread provider override to a *different* provider (e.g., `openrouter`) ignore the global base URL and use the provider's standard endpoint. This allows callable threads to route through OpenRouter while the main thread uses the proxy.

The global LLM settings are intentionally not account-scoped. Two users on the same Nymeria server cannot have different "global" providers; the last admin save wins for the deployment. To give one user's thread a different provider, configure that thread's LLM override instead.

**CLIProxyAPI tool name prefixing**: CLIProxyAPI can add a `proxy_` prefix to tool names with OAuth tokens. To disable this, add top-level `"tool_prefix_disabled": true` to the Claude OAuth token file in the auth directory (e.g., `~/.cli-proxy-api/claude-<email>.json`) and restart CLIProxy. `Nymeria/tools/check_cliproxy_cloak.py --auth-dir <auth-dir>` fails loudly when an active Claude auth file is missing the flag.

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
