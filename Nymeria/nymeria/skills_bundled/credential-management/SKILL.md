---
name: credential-management
description: How to manage user credentials, API keys, OAuth tokens, and service connections
  using Nymeria's auth tools. Use this skill whenever you need to request a new credential
  from the user (API key, OAuth login, PAT, or multi-field form), inspect existing
  credentials, clean up stale or duplicate credentials, or manage credential-to-target
  bindings for runtime secret resolution. Also use when a tool fails due to missing
  authentication, when you need to check if a credential exists before requesting
  a new one, or when connecting an MCP server that needs an API key.
metadata:
  nymeria:
    required_tools:
    - auth_inspect
    - auth_cleanup
    - auth_bindings
    - request_credential
    tool_ttl: 2h
---

# Credential Management

This skill teaches you how to use the four auth tools (`request_credential`, `auth_inspect`, `auth_cleanup`, `auth_bindings`) to manage the user's credentials vault. You never see secret values directly; all operations work through metadata and encrypted storage.

## Quick Decision Tree

```
Need a credential?
├─ Check if one exists first → auth_inspect(view="list", provider="...")
│  ├─ Active credential found → use it (or bind it if needed)
│  ├─ pending_setup found → tell user to finish setup, or clean up and re-request
│  └─ Nothing found → request_credential(...)
│
Need to see credential details/bindings?
└─ auth_inspect(view="status", credential_id="...")
│
Stale/duplicate credentials cluttering things up?
└─ auth_cleanup(operation="stale_oauth") or auth_cleanup(operation="disable_matching", ...)
│
Need to wire a credential to a tool or MCP server?
└─ auth_bindings(operation="bind", credential_id="...", target_type="...", target_id="...")
```

---

## Tool 1: `request_credential` — Ask the User for a Credential

This is fire-and-forget. It opens a floating panel in the user's chat, returns immediately, and the user fills it in on their own time. No automatic agent turn fires when they save.

### When to Use
- A tool call fails because of missing authentication
- You need an API key, OAuth login, personal access token, or multi-field credential
- An MCP server needs environment variables (API keys, tokens)

### Always Check First
Before requesting, check if a credential already exists:
```
auth_inspect(view="list", provider="the_provider")
```
If an active one exists, skip the request. If a `pending_setup` one exists, either tell the user to finish it or clean it up and re-request.

### Requesting an API Key (simplest case)
```
request_credential(
    provider="openweathermap",
    kind="api_key",
    display_name="OpenWeatherMap",
    description="API key for fetching weather data.",
    instructions="1. Go to https://openweathermap.org/api_keys\n2. Copy your API key\n3. Paste it below"
)
```
This creates a single `value` field. The user pastes their key and saves.

### Requesting with Custom Fields
For credentials with multiple parts (key + endpoint, username + password, etc.):
```
request_credential(
    provider="my_service",
    kind="api_key",
    display_name="My Service",
    description="Credentials for connecting to My Service API.",
    instructions="1. Log into your dashboard\n2. Go to Settings > API\n3. Copy the values below",
    fields=[
        {"name": "api_key", "label": "API Key", "secret": true, "placeholder": "sk-..."},
        {"name": "endpoint", "label": "Endpoint URL", "secret": false, "kind": "text", "placeholder": "https://api.example.com"},
        {"name": "service_json", "label": "Service Account JSON", "secret": true, "kind": "textarea", "placeholder": "{\"type\": ...}"}
    ]
)
```
Field `kind` values: `"password"` (masked, default when secret=true), `"text"` (plain), `"textarea"` (multiline, good for JSON blobs).

### Requesting a PAT or Form Credential
```
request_credential(provider="github", kind="pat", ...)
request_credential(provider="internal_app", kind="form", ...)
```
`pat` and `form` behave identically to `api_key` in terms of the modal. The `kind` value is stored on the credential for semantic distinction.

### Requesting OAuth
OAuth providers must be in the registry: `google_analytics`, `google_business_profile`, `google_calendar`, `google_docs`, `google_gmail`, `outlook`.
```
request_credential(
    provider="outlook",
    kind="oauth",
    account_label="work",
    flow="device_code"
)
```
- `flow="device_code"`: Shows an on-screen code. Works without NYMERIA_PUBLIC_URL. Preferred.
- `flow="auth_code"`: Browser redirect. Requires NYMERIA_PUBLIC_URL or `use_localhost=True`.
- If `auth_code` fails with `missing_public_url`, retry with `use_localhost=True` (only if user's browser is on the same machine) or switch to `device_code`.

### Auto-Binding with `bind_target`
To automatically bind the credential to a target when the user saves:
```
request_credential(
    provider="my_api",
    kind="api_key",
    bind_target="mcp_server:my-server-id"
)
```
Format: `"mcp_server:<id>"` or `"native_tool:<name>"`. The binding only takes effect after the user saves (not during `pending_setup`). For MCP servers, this also force-restarts the connection.

### What You Get Back
Success:
```json
{"ok": true, "status": "dispatched", "credential_id": "cred_...", "prompt_id": "prompt_...", "provider": "...", "timeout_seconds": 180}
```

Errors (ok=false):
| status | meaning |
|--------|---------|
| `missing_public_url` | OAuth auth_code needs NYMERIA_PUBLIC_URL or use_localhost |
| `unknown_provider` | Provider not in OAuth registry (use api_key instead) |
| `unsupported_flow` | Provider doesn't support that flow |
| `client_config_missing` | OAuth client credentials not configured |
| `device_code_request_failed` | Device code request failed at provider |

### After Dispatching (critical)
1. **Tell the user** a prompt appeared and what to do with it
2. **Never go silent** after dispatching
3. If user says "done" or "try again": retry the operation that needed the credential
4. If user pastes an error: help debug, don't silently retry
5. If user asks something else: do that; the modal stays open independently

---

## Tool 2: `auth_inspect` — Read Credential Metadata

Never exposes secret values. Three views available.

### List View (default)
```
auth_inspect(view="list")                                    # all active credentials
auth_inspect(view="list", provider="openai")                 # filter by provider
auth_inspect(view="list", kind="oauth_token")                # filter by kind
auth_inspect(view="list", status="pending_setup")            # filter by status
auth_inspect(view="list", provider="openai", status="active") # AND logic filters
auth_inspect(view="list", include_disabled=true)             # include disabled creds
auth_inspect(view="list", prompt_id="prompt_abc123")         # filter by creation prompt
auth_inspect(view="list", account_id="user_at_example_com")  # filter by OAuth account
```

**Important**: `include_disabled` defaults to `false`. Disabled credentials are invisible unless you explicitly ask for them. Always pass `include_disabled=true` when investigating cleanup or looking for credentials you know were disabled.

Filter values for `kind`: `"oauth_token"`, `"api_key"`, `"pat"`, `"form"`, `"legacy_token_cache"`
Filter values for `status`: `"active"`, `"pending_setup"`, `"disabled"`

Returns `{ok, credentials: [...], total}`.

### Status View (single credential deep-dive)
```
auth_inspect(view="status", credential_id="cred_abc123")
```
Returns `{ok, credential: {...}, bindings: [...]}`. This is the only way to see binding details (binding IDs, target types, target IDs).

### OAuth Accounts View
```
auth_inspect(view="oauth_accounts")
```
Groups credentials by provider account. Returns `{ok, accounts: [...], total_credentials}`. Each account shows `{provider, account_id, emails, credentials, active_count, pending_count, legacy_count}`. Only shows active (non-disabled) accounts.

### Key Fields on a Credential Object
- `id`: `"cred_..."` (user-created) or `"cred_lcache_..."` (legacy cache import)
- `status`: `"active"` | `"pending_setup"` | `"disabled"`
- `has_secret`: `true` when the user has saved a secret value; `false` while `pending_setup`
- `allowed_targets`: Array like `["native_tool:*"]` showing what can resolve this credential
- `metadata.source`: `"request_credential"`, `"oauth_callback"`, `"oauth_device_flow"`, or `"auth_cache_utils"`
- `secret_fields`: Names of encrypted fields (e.g. `["value"]`, `["access_token", "refresh_token"]`). Values are never returned.

---

## Tool 3: `auth_cleanup` — Disable Stale or Unwanted Credentials

All cleanup is soft-delete (sets status to `disabled`). Credentials still exist but become inactive.

### Clean Up Stale OAuth
```
auth_cleanup(operation="stale_oauth")                  # dry run (preview only)
auth_cleanup(operation="stale_oauth", dry_run=false)   # actually disable
```
Finds: pending OAuth placeholders that were never completed, and legacy token caches replaced by newer OAuth tokens. Returns `{ok, candidates: [...], disabled: [...]}`.

### Disable by Filter
```
auth_cleanup(operation="disable_matching", provider="test_service")                          # dry run
auth_cleanup(operation="disable_matching", provider="openai", status="pending_setup", dry_run=false)  # live
```
Requires at least one filter (`provider`, `kind`, `status`, `account_id`, `prompt_id`). Filters combine with AND logic. Returns `{ok, matched: [...], matched_count, disabled: [...]}`.

**Both `stale_oauth` and `disable_matching` are dry run by default.** You must explicitly pass `dry_run=false` to actually disable anything. Always preview first, then confirm with the user before disabling.

### Disable a Single Credential
```
auth_cleanup(operation="disable", credential_id="cred_abc123")
```
Immediate, no dry run. Returns `{ok: true, disabled: true}`.

---

## Tool 4: `auth_bindings` — Wire Credentials to Tools/Servers

Bindings control which tools and MCP servers can resolve a credential at runtime.

### Bind
```
auth_bindings(
    operation="bind",
    credential_id="cred_abc123",
    target_type="mcp_server",        # or "native_tool"
    target_id="my-server-id",        # server ID or tool name
    binding_name="my-api-binding"    # optional human label
)
```
Returns `{ok: true, binding_id: "cbind_..."}`. Adds the target to the credential's `allowed_targets` array.

### Unbind
```
auth_bindings(operation="unbind", binding_id="cbind_abc123")
```
Returns `{ok: true, deleted: true, allowed_target_removed: true/false}`. Removes the binding record. If no other binding exists for the same credential+target, also removes the `allowed_targets` entry (revoking runtime access).

### When to Bind
- OAuth tokens auto-bind to `native_tool:*` on creation. Usually no manual binding needed.
- API keys have empty `allowed_targets` by default. Bind them to the tool or MCP server that needs them.
- Use `bind_target` on `request_credential` to auto-bind on save instead of binding manually after.

### Checking Bindings
Use `auth_inspect(view="status", credential_id="...")` to see the `bindings[]` array with full details. The `list` view only shows `allowed_targets` (the permission entries) but not individual binding records.

---

## Credential Lifecycle

```
request_credential()
    → pending_setup  (has_secret: false, user hasn't saved yet)
    → active         (has_secret: true, user saved the value)
    → disabled       (soft-deleted via auth_cleanup)
```

For OAuth:
```
request_credential(kind="oauth")
    → pending_setup  (oauth_pending: true, waiting for OAuth flow)
    → active         (access_token + refresh_token stored, scopes populated)
    → disabled       (replaced by newer token, or manually cleaned up)
```

## Common Patterns

### Pattern: Tool fails, need to request credentials
1. `auth_inspect(view="list", provider="the_service")` to check for existing credentials
2. If none found: `request_credential(provider="the_service", kind="api_key", ...)` with helpful `description` and `instructions`
3. Tell the user the prompt appeared and to let you know when done
4. When user confirms: retry the original operation

### Pattern: Connect an MCP server
1. `request_credential(provider="the_api", kind="api_key", bind_target="mcp_server:server-id", ...)`
2. The credential auto-binds to the MCP server and force-restarts the connection on save

### Pattern: Clean up credential clutter
1. `auth_cleanup(operation="stale_oauth")` to preview stale OAuth
2. `auth_cleanup(operation="disable_matching", status="pending_setup", provider="...")` to preview abandoned setups
3. Confirm with user, then re-run with `dry_run=false`

### Pattern: Check which accounts are connected
1. `auth_inspect(view="oauth_accounts")` for a clean per-provider-account summary

## Safety

Credential work touches secrets, so the safety defaults are built into the tools:
inspect never returns secret values, cleanup is dry-run by default, and
`request_credential` lets the user enter secrets themselves so you never handle raw
values. Keep to those defaults: preview cleanups before running them with
`dry_run=false`, confirm before disabling or unbinding anything the user still
relies on, and never paste a secret value into a tool argument or message.

## Not the right kit?

- The tool or server that needs the credential does not exist yet -> enable or build
  it first via `tool-management`, or install it via `mcp-management`, then return
  here to authenticate it.
- You want to save a recurring authenticated workflow as reusable instructions ->
  `skill-management`.
- For the overall operating philosophy and when to create which artifact ->
  `Skill(name="self-improve")`.
