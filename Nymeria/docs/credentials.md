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
| `credential_secret_fields` | Actual secrets — each is a named field (e.g. `api_key`, `token`) stored as Fernet ciphertext, fully separate from metadata |
| `credential_bindings` | Explicit links between a credential and a consumer (e.g. "this Todoist key is for the `todoist_tasks` tool") |
| `credential_audit_events` | Immutable log of every action: created, used, bound, tested, disabled, decrypt failures |

### Credential Fields

- `owner_type`: `user` or `system`
- `owner_user_id`: set for user-owned credentials
- `provider` / `kind`: examples include `todoist` / `api_key`, `google_calendar` / `legacy_token_cache`, `mcp` / `env_var`
- `metadata`: arbitrary JSON (account info, source tracking)
- `scopes`: OAuth scope list (for OAuth-based credentials)
- `allowed_targets`: list of target strings that controls who can decrypt (see Access Control)
- `status`: `active`, `pending_setup`, or `disabled`
- `secret_fields`: list of field names only — plaintext and ciphertext are never exposed in API responses

## How Secrets Enter the Vault

There are four paths:

### 1. Settings > Connections UI (Primary)

The user types their secret into the desktop or mobile app. The frontend calls
`POST /credentials` with plaintext `secret_fields`. The API encrypts them with
Fernet and stores the ciphertext. The plaintext is never persisted.

### 2. Agent-Initiated Setup

The agent calls the `request_credential` tool to create a placeholder
credential with `status: pending_setup`. The agent never handles actual
secrets. The user completes the setup in a server-hosted form:

- Desktop receives the existing `auth_prompt` event and opens the credential
  modal.
- Chat bots receive an `auth_prompt` event containing a one-time setup link
  shaped as `${NYMERIA_PUBLIC_URL}/connect/credentials/{prompt_id}#<token>`.

The prompt token is stored only as a server-side hash, is scoped to one prompt,
and expires with the prompt. It lives in the URL fragment so normal HTTP
requests and access logs see only `/connect/credentials/{prompt_id}`. The
browser form copies the fragment token into an `Authorization: Bearer` header
when calling the prompt-token API endpoints.

If `NYMERIA_PUBLIC_URL` is unset, desktop prompts still work, but text-chat
surfaces report that a public URL is required and warn users not to paste
secrets into chat.

### 3. Auto-Migration at Startup

When the agent boots (`agent.py`), two migration functions run automatically:

- `migrate_auth_token_files()` — finds old per-user JSON files in
  `data/auth_tokens/<user_id>/` and moves them into the vault as
  `legacy_token_cache` credentials. Provider-specific files under `mcp/`
  subdirectories are left in place because third-party MCP servers still read
  those files directly.
- `migrate_mcp_encrypted_env_vars()` — converts legacy `encrypted_env_vars`
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

Tools never hardcode API keys. There are four resolution paths depending on
tool type.

### Path 1: Native Tools

There are ~34 service integration files (e.g. `productivity_service_integrations.py`,
`project_management_service_integrations.py`). Each tool has a helper that
calls:

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

The scoring also considers **thread-level targeting** — you can store one
OpenAI key scoped to `thread:<thread-id>` and a different one as the global
fallback. The resolver also reads `base_url` from secret fields or metadata,
supporting custom endpoints per credential.

Save a credential with `provider=<LLM provider ID>` (e.g. `openai`,
`openrouter`, `anthropic`), `kind=api_key`, secret field `api_key`, and
allowed target `llm_provider:<provider>` or `llm_provider:*`.

## Access Control

- **Ownership**: Each credential is `user`-owned (scoped to one user) or
  `system`-owned (shared). User credentials are only visible to their owner.
  Repository methods also enforce owner/admin checks when decrypting,
  disabling, or deleting user-owned credentials, so callers do not rely only
  on router-level guards.
- **Allowed targets**: A list like `["native_tool:todoist_tasks", "mcp_server:my-server"]`
  restricts which consumers can decrypt. `"*"` or empty list = unrestricted.
  Supported target strings include `native_tool:<name>`, `mcp_server:<id>`,
  `custom_tool:<id>`, `llm_provider:<provider>`, `thread:<id>`, and wildcard
  forms like `native_tool:*`.
- **Bindings**: Explicit credential-to-target links stored in the
  `credential_bindings` table. Bindings override the scoring system (highest
  priority).
- **Status**: `active`, `pending_setup`, `disabled`. Disabled credentials
  are never returned by the resolution functions.
- **Agent safety**: The `auth_manager` tool that the agent uses never returns
  plaintext secrets, ciphertext, or partial keys. It can list metadata, create
  placeholders, bind credentials, and disable them — but actual secret entry
  must happen through the UI or authenticated API.

## Agent Access — `auth_manager` Tool

The optional `auth_manager` tool provides metadata-only credential management
from within chat. It supports these actions:

| Action | What it does |
|--------|-------------|
| `list` | List the current user's credentials plus system credential metadata |
| `status` | Show metadata and bindings for one credential |
| `request_setup` | Create a pending setup record for the user to complete in the UI |
| `bind` | Bind a credential to a target (e.g. `mcp_server:my-server`) |
| `unbind` | Remove a binding by binding ID |
| `test` | Run a vault health check (confirms secret fields exist, no plaintext) |
| `disable` | Disable a credential |

The agent cannot manage system credentials or retrieve plaintext secrets.

## REST API

| Route | Method | Purpose |
|-------|--------|---------|
| `/credentials` | GET | List credentials (`scope=visible\|mine\|system\|all`) |
| `/credentials` | POST | Create credential (accepts `secret_fields` as plaintext) |
| `/credentials/{id}` | GET | Get one credential's metadata |
| `/credentials/{id}` | PATCH | Update credential metadata or secret fields |
| `/credentials/{id}` | DELETE | Delete credential and all bindings |
| `/credentials/{id}/test` | POST | Test credential (provider-specific health check) |
| `/credentials/{id}/bindings` | GET | List bindings for a credential |
| `/credentials/{id}/bindings` | POST | Create a new binding |
| `/credential-bindings/{id}` | DELETE | Remove a binding |
| `/credential-setup-sessions` | POST | Create a setup session (agent workflow) |
| `/credential-prompts/{id}/test` | POST | Test fields for an active desktop prompt |
| `/credential-prompts/{id}/submit` | POST | Save fields for an active desktop prompt |
| `/credential-prompts/{id}/exit` | POST | Close a desktop prompt with an optional note |
| `/credential-prompts/{id}/cancel` | POST | Cancel a desktop prompt with an optional note |
| `/connect/credentials/{id}` | GET | Hosted credential form shell for chat-bot links |
| `/connect/credentials/{id}/prompt` | GET | Prompt-token metadata endpoint |
| `/connect/credentials/{id}/test` | POST | Prompt-token provider test endpoint |
| `/connect/credentials/{id}/submit` | POST | Prompt-token save endpoint |
| `/connect/credentials/{id}/exit` | POST | Prompt-token close endpoint |
| `/connect/credentials/{id}/cancel` | POST | Prompt-token cancel endpoint |

Writes accept `secret_fields` as plaintext. Responses only return metadata
and the list of stored secret field names — never plaintext or ciphertext.
The `/connect/credentials/*` API endpoints authenticate with the one-time
prompt token from the hosted setup link and do not grant general API access.

## Provider Tests

Credential verification is handled by `nymeria/core/credential_tests.py`.
Provider testers receive provider/kind/metadata plus the submitted plaintext
fields inside the API process and return a redacted status object:
`ok`, `message`, `code`, and `verified`.

The initial registry includes probes for GitHub, Todoist, Anthropic, and known
OpenAI-compatible LLM providers. If a provider has no tester, Nymeria saves the
credential as active but returns `tested=false` and `test_status=not_verified`
with an explicit "no verification probe yet" message rather than pretending the
provider accepted the key.

The desktop prompt has separate **Test** and **Save** actions. Save re-runs the
provider test before resolving the agent tool; failed tests stay in the prompt
so the user can retry.

## Example End-to-End Flow

Using the Todoist tool as an example:

1. You go to **Settings > Connections** in the desktop/mobile app
2. You add a credential: provider = `todoist`, field = `api_key`, value = your
   Todoist API key, allowed target = `native_tool:*`
3. The UI calls `POST /credentials` — the vault encrypts the key with Fernet
   and stores the ciphertext
4. Later, in chat, the agent calls the `todoist_tasks` tool
5. Inside the tool, `get_native_credential_value(provider="todoist", ...)`
   fires
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
  for credential setup links in chat bots. Localhost HTTP is acceptable only
  for local development.
- Fernet key rotation requires decrypting and re-encrypting all stored values.
  There is no automated rotation command yet.
- Audit events are append-only and include the credential ID, actor, event
  type, target, and timestamp. They are never pruned automatically.

## Planned: Password Guard for Token Operations

The credential vault itself is properly gated — all API endpoints require a
valid bearer token. However, the CLI (`python run.py users rotate-token`)
bypasses the API and writes directly to SQLite with no authentication. On
shared or bare-metal production installs, this means any local user could
mint an admin token and access all vault credentials.

The mitigation is an optional account password (the `password_hash` column
already exists in the `users` table). When set, token-issuing operations
(CLI `rotate-token`, API `POST /me/tokens`) require the password. An email
reset flow covers the lockout scenario. A `REQUIRE_ACCOUNT_PASSWORD` setting
lets production admins enforce this while solo installs stay frictionless.

Full details in [`accounts.md`](accounts.md).

## Key Source Files

| File | Role |
|------|------|
| `nymeria/core/credential_vault.py` | Vault repo, schema, encrypt/decrypt, reference resolution, migrations |
| `nymeria/core/auth_prompt_coordinator.py` | In-process prompt futures, one-time prompt tokens, and prompt lookup |
| `nymeria/core/credential_tests.py` | Provider test registry and redacted probe helpers |
| `nymeria/core/llm_credentials.py` | LLM provider credential resolution from the vault |
| `nymeria/tools/native_credentials.py` | Native tool credential resolution |
| `nymeria/tools/auth_manager.py` | Agent-facing metadata-only tool |
| `nymeria/tools/credential_prompt.py` | Agent-facing `request_credential` tool |
| `nymeria/tools/auth_cache_utils.py` | OAuth token cache I/O (vault-first, file fallback) |
| `nymeria/core/custom_tools.py` | `${credential:...}` resolution in custom HTTP tools |
| `nymeria/core/mcp_manager.py` | `${credential:...}` resolution in MCP server config |
| `nymeria/api/routers/credentials.py` | REST API router |
| `nymeria/api/routers/credential_prompts.py` | Desktop and hosted-form prompt resolution endpoints |
| `nymeria/api/schemas/credentials.py` | Pydantic request/response schemas |
| `tests/test_credential_vault.py` | Vault unit tests |
| `tests/test_credential_prompt_flow.py` | Prompt flow integration tests |
| `tests/test_credential_tests.py` | Provider test registry tests |
