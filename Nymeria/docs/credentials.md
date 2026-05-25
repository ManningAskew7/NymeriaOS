# Credential Vault

Nymeria stores reusable tool credentials in an encrypted server-side vault
backed by `data/accounts.db`. Secret fields are Fernet-encrypted using the
`NYMERIA_SECRETS_KEY` environment variable via `nymeria/core/secrets.py`.

This document covers the full surface of the credential vault: data model,
how secrets enter the vault, how tools automatically receive their
credentials at runtime, access control, and the agent-facing tool.

## Data Model

The vault uses four SQLite tables:

| Table | Purpose |
|-------|---------|
| `credentials` | Public metadata: name, provider, kind, status, allowed targets, ownership |
| `credential_secret_fields` | Actual secrets - each is a named field (e.g. `api_key`, `token`) stored as Fernet ciphertext, fully separate from metadata |
| `credential_bindings` | Explicit links between a credential and a consumer (e.g. "this Todoist key is for the `todoist_tasks` tool") |
| `credential_audit_events` | Immutable log of every action: created, used, bound, tested, disabled, decrypt failures |

### Credential Fields

- `owner_type`: `user` or `system`
- `owner_user_id`: set for user-owned credentials
- `provider` / `kind`: examples include `todoist` / `api_key`, `google_calendar` / `oauth_token`, `google_calendar` / `legacy_token_cache`, `mcp` / `env_var`
- `metadata`: arbitrary JSON (account info, source tracking)
- `scopes`: OAuth scope list (for OAuth-based credentials)
- `allowed_targets`: list of target strings that controls who can decrypt (see Access Control)
- `status`: `active`, `pending_setup`, `invalid`, or `disabled`
- `secret_fields`: list of field names only - plaintext and ciphertext are never exposed in API responses

## How Secrets Enter the Vault

There are four paths:

### 1. Settings > Connections UI (Primary)

The user types their secret into the desktop or mobile app. The frontend calls
`POST /credentials` with plaintext `secret_fields`. The API encrypts them with
Fernet and stores the ciphertext. The plaintext is never persisted.

### 2. Agent-Initiated Setup

The agent calls the `request_credential` tool to create a placeholder
credential with `status: pending_setup`. The agent never handles actual
secrets. The user completes setup through an app modal or hosted form:

- Desktop receives the existing `auth_prompt` event and opens the floating
  `AuthPromptModal`. Mobile currently has the Settings > Connections manager
  but no global `auth_prompt` modal.
- Chat bots receive an `auth_prompt` event containing a one-time setup link
  shaped as `${NYMERIA_PUBLIC_URL}/connect/credentials/{prompt_id}#<token>`.

The prompt token is stored only as a server-side hash, is scoped to one prompt,
and expires with the prompt. It lives in the URL fragment so normal HTTP
requests and access logs see only `/connect/credentials/{prompt_id}`. The
browser form copies the fragment token into an `Authorization: Bearer` header
when calling the prompt-token API endpoints.

If `NYMERIA_PUBLIC_URL` is unset, desktop API-key/PAT/form prompts still work
because the modal uses authenticated `/credential-prompts/*` API calls. Text
chat surfaces cannot build hosted setup links without a public URL and warn
users not to paste secrets into chat. OAuth authorization-code prompts also
need a browser-reachable callback URL unless the agent retries with
`use_localhost=True`.

**Fire-and-forget tool contract:** `request_credential` returns immediately
with `status="dispatched"`. The agent does not block waiting for the user and
no automatic follow-up turn fires when the prompt is submitted, cancelled, or
swept. The coordinator future still resolves inside the API process so the
done-callback can cancel any device-code poller, apply a valid `bind_target`,
restart an MCP connection when needed, and log the result. The user drives the
next step by saying something like "done" or "try again".

For OAuth prompts, ordinary chat messages such as "done" do not resolve or
cancel the prompt. Authorization-code callbacks and device-code pollers are the
only success paths. This keeps the backend listening after the user grants
provider permissions and prevents a premature chat reply from cancelling the
device-code poller.

The agent should write a short acknowledgement after dispatching the prompt,
ask the user to finish the prompt, and ask them to ping the agent when done or
paste any inline error back into chat.

**Two new optional args on `request_credential`:**

- `instructions` (string, markdown): step-by-step guidance shown in a
  highlighted callout above the form. Different from `description`, which is
  the short "what is this" summary. Use `instructions` to tailor steps to the
  user's context, e.g. "Since you mentioned you're a Resend customer, find
  your key at resend.com/api-keys, Create new, then paste it below."
- `bind_target` (string `"type:id"`): automatic post-save binding. Currently
  supports `"mcp_server:<id>"` (binds the credential to a specific MCP server
  and force-restarts that server's connection so the next tool call resolves
  the new env value) and `"native_tool:<name>"` (scopes the credential to one
  native tool). Bind failures are non-fatal: the credential still saves and
  the failure is logged. Validation regex:
  `^(mcp_server|native_tool):[A-Za-z0-9_-]{1,64}$`.

### 3. Auto-Migration at Startup

When the agent boots (`agent.py`), two migration functions run automatically:

- `migrate_auth_token_files()` - finds old per-user JSON files in
  `data/auth_tokens/<user_id>/` and moves them into the vault as
  `legacy_token_cache` credentials. Provider-specific files under `mcp/`
  subdirectories are left in place because third-party MCP servers still read
  those files directly.
- `migrate_mcp_encrypted_env_vars()` - converts legacy `encrypted_env_vars`
  in MCP server configs into vault credentials. The original server definition
  is rewritten to use `${credential:id.value}` references. If decryption or
  import fails, the original value is left untouched and a warning is logged.

### 4. Managed MCP Install

The MCP preview/install flow scans pasted server definitions for secret-like
env vars and HTTP headers by name and value. During install:

- provided secret values are written as user-owned vault credentials with
  `provider=mcp`, `kind=secret`, and `allowed_targets=["mcp_server:<server_id>"]`
- server JSON stores only `${credential:<id>.value}` references
- missing required secrets create `pending_setup` credentials instead of
  placeholder values
- if `NYMERIA_SECRETS_KEY` is missing, the install is saved as `needs_config`
  with pending setup records rather than persisting plaintext

The agent-facing `manage_mcp` tool never accepts or returns plaintext MCP
secrets. It can create or bind pending setup records, then directs the user to
finish in **Settings > Connections**.

## How Tools Automatically Get Their Credentials

Tools never hardcode API keys. There are five resolution paths depending on
tool type.

### Path 1: Native Tools

Service integration modules (e.g. `productivity_service_integrations.py`,
`project_management_service_integrations.py`) use helpers that call:

```python
get_native_credential_value(
    provider="todoist",
    field_names=("api_key", "token"),
    tool_name="todoist_tasks",
    config=config,
)
```

The resolver (`native_credentials.py`):

1. Lists all active vault credentials matching that provider name
2. Checks for explicit **bindings** (highest priority, score 0)
3. Checks **allowed_targets** for `native_tool:<tool_name>` (score 1)
4. Falls back to wildcard `native_tool:*` or `*` targets (score 2)
5. Falls back to unscoped credentials with no targets (score 3)
6. At the same priority level, prefers user-owned over system-owned
7. Decrypts the first matching secret field, logs an audit event, returns it

If no credential matches, the tool returns a human-readable setup hint:

> No Todoist credential found. Save one in Settings > Connections with
> provider "todoist", required field(s) "api_key", and allowed target
> "native_tool:todoist_tasks" or "native_tool:*".

### Path 2: Custom HTTP Tools

When a custom tool's URL, headers, or body contains `${credential:my_cred.api_key}`,
the custom tool executor (`custom_tools.py`) calls `vault.resolve_references()`
at runtime. This regex-matches every `${credential:X.Y}` placeholder, decrypts
field `Y` from credential `X`, and substitutes the plaintext inline. It also
tracks resolved values so tool output can be **redacted** (secrets scrubbed
from responses).

### Path 3: MCP Server Environment and Headers

Same `${credential:id.field}` pattern. When an MCP server starts, the MCP
manager (`mcp_manager.py`) resolves these references in env vars and HTTP
headers before spawning the process.

Managed MCP installs use this path by default. New installs should not use
legacy `encrypted_env_vars`; that field is only retained so old definitions can
continue to boot until startup migration rewrites them.

### Path 4: LLM Provider Keys

`llm_credentials.py` handles this path separately from native tools. When
building an LLM graph, it searches for vault credentials with:

- `kind` in `{api_key, llm_provider, llm_connection, provider_connection, openai_compatible, connection}`
- `provider` matching the target LLM provider (normalized)

The scoring also considers **thread-level targeting** - you can store one
OpenAI key scoped to `thread:<thread-id>` and a different one as the global
fallback. The resolver also reads `base_url` from secret fields or metadata,
supporting custom endpoints per credential.

Save a credential with `provider=<LLM provider ID>` (e.g. `openai`,
`openrouter`, `anthropic`), `kind=api_key`, secret field `api_key`, and
allowed target `llm_provider:<provider>` or `llm_provider:*`.

### Path 5: OAuth via `request_credential`

For services that require an OAuth 2.0 sign-in (Google Calendar, Gmail, Google
Docs/Drive/Sheets, Google Analytics, Google Business Profile, Microsoft
Outlook), the agent calls `request_credential` with `kind="oauth"` and the
relevant `provider` (e.g. `google_calendar`). The provider registry lives in
`nymeria/config/oauth_providers.py` and pins the authorize URI, token URI,
scope set, client-config source, and which flows the provider supports.

Two flows are wired in:

- **Authorization code** (`mode="oauth"` in the SSE event). Default for
  desktop and any bot user where `NYMERIA_PUBLIC_URL` is reachable. The
  agent's tool builds an authorization URL with PKCE (when the descriptor
  enables it) and emits an `auth_url` the modal opens in the user's browser.
  The browser is redirected back to
  `${NYMERIA_PUBLIC_URL}/connect/credentials/oauth/callback?code=<code>&state=<state>`,
  which exchanges the code for tokens and writes the vault record. The
  redirect URI path is **static** so the user only registers one URI per
  origin in their Google Cloud Console / Azure Portal - `prompt_id` and a
  one-time nonce are packed into the OAuth `state` parameter
  (`"<prompt_id>:<nonce>"`). The callback handler unpacks the state, looks
  up the prompt by id, then `hmac.compare_digest` checks the nonce against
  `metadata["_oauth_state"]["state_token_hash"]`. The hash is distinct from
  the hosted-form bearer token so the state value exposed in the browser
  URL never doubles as a form-access credential.
- **Device code** (`mode="oauth_device"`, RFC 8628). Used when the agent
  passes `flow="device_code"` or when `NYMERIA_PUBLIC_URL` is unset and the
  provider supports device-flow (currently Microsoft Outlook). The tool POSTs
  to the device-authorization endpoint, emits a short `user_code` and
  `verification_uri`, and spawns a background poller against the token URI.
  When the user finishes the sign-in on any device, the poller resolves the
  same coordinator future the auth-code callback would resolve. The poll
  task lives in a module-level registry (`oauth_device_flow._POLL_TASKS`),
  not on `PendingPrompt.metadata`, because `asyncio.Task` is not JSON
  serialisable.

When `NYMERIA_PUBLIC_URL` is not configured and the provider does not
support device-code, the tool returns `status="missing_public_url"` without
emitting a prompt. The agent can retry with `use_localhost=True` after
confirming with the user; that path is only safe when the user's browser is
on the same machine as Nymeria.

The vault record written by both flows uses these conventions:

- `kind = "oauth_token"`
- `provider`: matches the descriptor ID (e.g. `google_calendar`,
  `google_gmail`, `outlook`)
- `secret_fields`: `access_token` (always), `refresh_token` (when the
  provider returns one). `client_secret` is read from env / Google client
  config at refresh time and is **not** stored in the vault.
- `metadata`: `scopes` (list), `email`, `name`, `account_id`, `expires_at`
  (ISO 8601, UTC), `client_id`, `token_uri`, `provider_id`, `source`
  (`"oauth_callback"` or `"oauth_device_flow"`)
- `allowed_targets`: default `["native_tool:*"]`, so any native Google or
  Outlook tool can read the token. Bind to a specific tool name to scope.

When a reconnect completes for the same provider account, Nymeria promotes the
new prompt credential to active, carries over allowed targets and bindings from
older duplicate rows, and disables stale same-account OAuth rows plus abandoned
pending OAuth placeholders for that provider. Legacy token-cache rows for the
same account are disabled once an active vault OAuth token replaces them.

Tools that consume OAuth tokens (`google_docs.get_credentials`,
`calendar.py`, `outlook_email.py`, etc.) call
`auth_cache_utils.resolve_oauth_cache(user_id, provider)`, which merges
vault `oauth_token` rows with the legacy `data/auth_tokens/<user_id>/*.json`
cache. Vault accounts win on `account_id` collision; legacy-only accounts
remain visible until the user re-auths via the new flow. Refreshes write
back to whichever store the account originated from: vault rows go through
`upsert_credential`, legacy rows through `save_token_cache`. The
`_vault_credential_id` sentinel on each merged account routes the persist
call to the correct store.

## Access Control

- **Ownership**: Each credential is `user`-owned (scoped to one user) or
  `system`-owned (shared). User credentials are only visible to their owner.
  Repository methods also enforce owner/admin checks when decrypting,
  disabling, or deleting user-owned credentials, so callers do not rely only
  on router-level guards.
- **Allowed targets**: A list like `["native_tool:todoist_tasks", "mcp_server:my-server"]`
  restricts which consumers can decrypt. `"*"` or empty list = unrestricted.
  Supported target strings include `native_tool:<name>`, `mcp_server:<id>`,
  `custom_http_tool:<id>`, `llm_provider:<provider>`, `thread:<id>`, and
  wildcard forms like `native_tool:*`.
- **Bindings**: Explicit credential-to-target links stored in the
  `credential_bindings` table. Bindings override the scoring system (highest
  priority).
- **Status**: `active`, `pending_setup`, `invalid`, `disabled`. Runtime
  resolution functions use active credentials only.
- **Agent safety**: The `auth_manager` tool that the agent uses never returns
  plaintext secrets, ciphertext, or partial keys. It can list metadata, create
  placeholders, bind credentials, and disable them - but actual secret entry
  must happen through the UI or authenticated API.

## Agent Access - `auth_manager` Tool

The `auth_manager` tool (default-enabled on every new thread) provides
metadata-only credential management from within chat. It supports these
actions:

| Action | What it does |
|--------|-------------|
| `list` | List the current user's credentials plus system credential metadata, with optional provider/kind/status/account/prompt filters |
| `status` | Show metadata and bindings for one credential |
| `oauth_accounts` | Group OAuth and legacy token-cache credentials by provider account so the agent can see duplicates and pending rows |
| `cleanup_stale_oauth` | Dry-run by default. Finds stale pending OAuth prompts, duplicate active OAuth tokens, and legacy token caches replaced by active vault OAuth credentials; pass `dry_run=false` to disable the candidates |
| `disable_matching` | Dry-run by default. Disable credentials matching metadata filters such as provider, kind, status, account ID, or prompt ID |
| `request_setup` | Create a pending setup record for the user to complete in the UI |
| `bind` | Bind a credential to a target (e.g. `mcp_server:my-server`) and update allowed targets |
| `unbind` | Remove a binding by binding ID |
| `test` | Run a vault health check (confirms secret fields exist, no plaintext) |
| `disable` | Disable a credential |

The agent cannot manage system credentials or retrieve plaintext secrets.

## REST API

| Route | Method | Purpose |
|-------|--------|---------|
| `/credentials` | GET | List credentials (`scope=visible\|mine\|system\|all`) |
| `/credentials` | POST | Create credential (accepts `secret_fields` as plaintext) |
| `/credentials/{credential_id}` | GET | Get one credential's metadata |
| `/credentials/{credential_id}` | PATCH | Update credential metadata or secret fields |
| `/credentials/{credential_id}` | DELETE | Disable by default, or hard-delete with `?hard=true` |
| `/credentials/{credential_id}/test` | POST | Test credential (provider-specific health check) |
| `/credentials/{credential_id}/bindings` | GET | List bindings for a credential |
| `/credentials/{credential_id}/bindings` | POST | Create a new binding |
| `/credential-bindings/{binding_id}` | DELETE | Remove a binding |
| `/credential-setup-sessions` | POST | Create a setup session (agent workflow) |
| `/credential-prompts/{prompt_id}/test` | POST | Test fields for an active desktop prompt |
| `/credential-prompts/{prompt_id}/status` | GET | Check whether an active or recently resolved desktop prompt is pending, active, invalid, disabled, or missing |
| `/credential-prompts/{prompt_id}/submit` | POST | Save fields for an active desktop prompt |
| `/credential-prompts/{prompt_id}/exit` | POST | Close a desktop prompt with an optional note |
| `/credential-prompts/{prompt_id}/cancel` | POST | Cancel a desktop prompt with an optional note |
| `/connect/credentials/{prompt_id}` | GET | Hosted credential form shell for chat-bot links |
| `/connect/credentials/{prompt_id}/prompt` | GET | Prompt-token metadata endpoint |
| `/connect/credentials/{prompt_id}/test` | POST | Prompt-token provider test endpoint |
| `/connect/credentials/{prompt_id}/submit` | POST | Prompt-token save endpoint |
| `/connect/credentials/{prompt_id}/exit` | POST | Prompt-token close endpoint |
| `/connect/credentials/{prompt_id}/cancel` | POST | Prompt-token cancel endpoint |
| `/connect/credentials/oauth/callback` | GET | Static OAuth authorization-code redirect target. Unpacks `prompt_id` + nonce from `state`, verifies the nonce, exchanges the code, writes the vault, resolves the pending prompt, and renders a success/failure HTML page. |

Writes accept `secret_fields` as plaintext. Responses only return metadata
and the list of stored secret field names - never plaintext or ciphertext.
The `/connect/credentials/*` API endpoints authenticate with the one-time
prompt token from the hosted setup link and do not grant general API access.

## Provider Tests

Credential verification is handled by `nymeria/core/credential_tests.py`.
Provider testers receive provider/kind/metadata plus the submitted plaintext
fields inside the API process and return a redacted status object:
`ok`, `message`, `code`, and `verified`.

The initial registry includes probes for GitHub, Todoist, Anthropic, and known
OpenAI-compatible LLM providers. If a provider has no tester, the test result is
`ok=true`, `verified=false`, `code="no_tester"`, with an explicit "no
verification probe yet" message rather than pretending the provider accepted
the key. Prompt submit responses expose this as `tested=false` and
`test_status="not_verified"`; direct credential responses expose only the
credential metadata plus `last_tested_at`.

The desktop prompt has separate **Test** and **Save** actions. Save re-runs the
provider test before resolving the prompt; failed tests keep the prompt open so
the user can retry.

## Example End-to-End Flow

Using the Todoist tool as an example:

1. You go to **Settings > Connections** in the desktop/mobile app
2. You add a credential: provider = `todoist`, field = `api_key`, value = your
   Todoist API key, allowed target = `native_tool:*`
3. The UI calls `POST /credentials` - the vault encrypts the key with Fernet
   and stores the ciphertext
4. Later, in chat, the agent calls the `todoist_tasks` tool
5. Inside the tool, `get_native_credential_value` runs with
   `provider="todoist"`
6. The vault finds your credential, confirms it is active and allowed for
   `native_tool:todoist_tasks`, decrypts the key, logs an audit event, and
   returns the plaintext to the tool internals
7. The tool uses the key to call Todoist's API
8. Secret values are redacted from any tool output shown to the user

## Operational Notes

- If `NYMERIA_SECRETS_KEY` is missing, new vault secret writes fail. Auth-cache
  helpers fall back to legacy file storage so local dev flows do not break
  abruptly, but production deployments must configure the key.
- `NYMERIA_PUBLIC_URL` must point at the browser-reachable HTTPS API origin
  for credential setup links in chat bots and for OAuth authorization-code
  redirects. Localhost HTTP is acceptable only for local development (the
  agent must opt-in with `use_localhost=True` for OAuth). Outlook prompts
  auto-degrade to device-code when no public URL is configured; Google
  providers do not, because they require a "Limited Input Device" client
  type that the v1 setup does not register.
- Fernet key rotation requires decrypting and re-encrypting all stored values.
  There is no automated rotation command yet.
- Audit events are append-only and include the credential ID, actor, event
  type, target, and timestamp. They are never pruned automatically.

## Planned: Password Guard for Token Operations

The credential vault itself is properly gated - all API endpoints require a
valid bearer token. However, the CLI (`python3 run.py users issue-token` and
`rotate-token`) bypasses the API and writes directly to SQLite with no
authentication. On shared or bare-metal production installs, this means any
local user could mint an admin token and access all vault credentials.

The mitigation is an optional account password (the `password_hash` column
already exists in the `users` table). When set, token-issuing operations
(CLI issue/rotate commands, API `POST /me/tokens`) require the password. An
email reset flow covers the lockout scenario. A `REQUIRE_ACCOUNT_PASSWORD`
setting lets production admins enforce this while solo installs stay
frictionless.

Full details in [`accounts.md`](accounts.md).

## Key Source Files

| File | Role |
|------|------|
| `nymeria/core/credential_vault.py` | Vault repo, schema, encrypt/decrypt, reference resolution, migrations |
| `nymeria/core/auth_prompt_coordinator.py` | In-process prompt futures, one-time prompt tokens, and prompt lookup |
| `nymeria/core/credential_tests.py` | Provider test registry and redacted probe helpers |
| `nymeria/core/llm_credentials.py` | LLM provider credential resolution from the vault |
| `nymeria/core/mcp_auth_bridge.py` | Gmail MCP OAuth-token export and cleanup bridge |
| `nymeria/core/mcp_runtime.py` | Managed MCP install credential-reference creation |
| `nymeria/tools/native_credentials.py` | Native tool credential resolution |
| `nymeria/tools/auth_manager.py` | Agent-facing metadata-only tool |
| `nymeria/tools/credential_prompt.py` | Agent-facing `request_credential` tool |
| `nymeria/config/oauth_providers.py` | OAuth provider descriptor registry (URIs, scopes, supported flows) |
| `nymeria/core/oauth_start.py` | Start-side: resolve flow, build auth URL or device-code request |
| `nymeria/core/oauth_callback_handler.py` | Finalize OAuth: code exchange, userinfo, vault write, coordinator resolve |
| `nymeria/core/oauth_device_flow.py` | RFC 8628 device-code poller |
| `nymeria/tools/auth_cache_utils.py` | OAuth token cache I/O (vault-first, file fallback) |
| `nymeria/core/custom_tools.py` | `${credential:<id>.<field>}` resolution in custom HTTP tools |
| `nymeria/core/mcp_manager.py` | `${credential:<id>.<field>}` resolution in MCP server config |
| `nymeria/api/routers/credentials.py` | REST API router |
| `nymeria/api/routers/credential_prompts.py` | Desktop and hosted-form prompt resolution endpoints |
| `nymeria/api/schemas/credentials.py` | Pydantic request/response schemas |
| `nymeria-desktop/src/lib/components/credentials/AuthPromptModal.svelte` | Desktop floating credential prompt panel |
| `nymeria-desktop/src/lib/stores/autonomous.svelte.ts` | Desktop `auth_prompt` SSE dispatch into prompt state |
| `nymeria-desktop/src/lib/stores/authPrompt.svelte.ts` | Desktop active prompt state from SSE |
| `nymeria-desktop/src/lib/components/credentials/CredentialManagerPanel.svelte` and mobile counterpart | Settings > Connections manager |
| `nymeria-desktop/src/lib/services/api/credentials.ts` and mobile counterpart | Typed frontend credential API client |
| `nymeria-desktop/src/lib/types/index.ts` and mobile counterpart | Credential and auth-prompt frontend types |
| `tests/test_credential_vault.py` | Vault unit tests |
| `tests/test_credential_prompt_flow.py` | Prompt flow integration tests |
| `tests/test_credential_tests.py` | Provider test registry tests |
