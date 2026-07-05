# Accounts & Authentication

Nymeria is transitioning from implicit-single-user to a real multi-user model. This doc covers the **backend** account/token layer: the `AccountsRepo` data model, bootstrap flow, the `/me` and `/admin/users` HTTP surface, and the service-token pattern bots and background workers use. The **frontend** UX that wraps these endpoints (sidebar avatar, account menu / switcher, Settings > Account / Users tabs, copy-once token dialog, error-toast layer) is documented separately in [`frontend-accounts.md`](frontend-accounts.md). Read that one if you're touching `nymeria-{desktop,mobile}/src/lib/components/account/`.

## Model

Twelve account-related tables live in a dedicated SQLite database at
`<data_dir>/accounts.db`, split across repository classes that share the same
DB file:

- **`AccountsRepo`** (`core/accounts.py`): users, tokens, thread ownership, platform identities
- **`ChatBindingsRepo`** (`core/chat_bindings.py`): thread-platform bindings, bind codes, user-owned Telegram bots
- **`CredentialVaultRepo`** (`core/credential_vault.py`): encrypted reusable credentials, bindings, and audit events

| Table | Purpose |
|---|---|
| `users` | Accounts (`id`, `email`, `display_name`, `role`, `disabled`) |
| `user_tokens` | Bearer tokens (one user can have many, each independently revocable) |
| `thread_owners` | Maps `thread_id` to `user_id`. Populated by (a) the desktop's eager `POST /threads/{thread_id}/claim` on new-thread creation, (b) admin first-touch on `/chat` for personal-pattern threads, (c) non-admin first-touch on `/chat` (TOFU), and (d) the startup orphan-backfill sweep. See [Thread ownership](#thread-ownership) below. |
| `platform_identities` | Maps chat-platform user IDs to Nymeria accounts |
| `schema_meta` | Accounts DB schema metadata and migration watermarks |
| `thread_platform_bindings` | Per-thread chat-app bindings (e.g. `desktop thread <-> Telegram chat`). Unique on `(provider, thread_id)` and `(provider, platform_chat_id)`. The optional `user_telegram_bot_id` column is null when the binding is served by the shared bot, or the row id of a `user_telegram_bots` entry when served by a user-owned (BYO) bot. |
| `bind_codes` | Short-lived single-use codes the desktop wizard mints and the bot consumes. Discriminated by `kind` - `platform_link` (link a Telegram identity to a Nymeria account) or `thread_bind` (attach a chat to a thread). 10-min TTL, hashed at rest. |
| `user_telegram_bots` | User-owned BYO Telegram bots. Tokens are stored as Fernet ciphertext (`bot_token_ciphertext`); plaintext is only handed to the supervisor process inside the `nymeria-telegram-bot` container via the admin endpoint. Encrypted with `NYMERIA_SECRETS_KEY`. |
| `credentials` / `credential_secret_fields` / `credential_bindings` | Encrypted reusable tool credentials and connection auth. Secret fields are Fernet ciphertext; public APIs return metadata only. See [`credentials.md`](credentials.md). |
| `credential_audit_events` | Credential-vault audit log rows for create/update/test/delete and binding operations |

Roles: `user` and `admin`. Admins can use the `X-Nymeria-Act-As` header to call the API on behalf of another user - used by bots, the ticker, and the watchdog.

### Tokens

Raw token format: `nym_<32-url-safe-bytes>`. Only `sha256(raw)` is stored. The raw value is returned **once** at creation and cannot be recovered - lost tokens must be rotated.

Tokens expire automatically. New regular tokens default to a 90-day TTL
(`ACCOUNT_TOKEN_TTL_DAYS`), and each user defaults to at most 10 active
non-expired tokens (`ACCOUNT_MAX_ACTIVE_TOKENS_PER_USER`). Expired tokens are
auto-revoked during token verification and token-list operations. First-run
bootstrap tokens use a shorter 24-hour TTL
(`ACCOUNT_BOOTSTRAP_TOKEN_TTL_HOURS`) and the plaintext
`BOOTSTRAP_TOKEN.txt` file is deleted after the first successful
authentication with that token.

Token redaction is applied at the logging layer (`config/logging_config.py::_TokenRedactingFilter`), so accidentally logged tokens come out as `nym_<redacted>`.

### Token and key quick reference

| Value | Used for | Prefix / location |
|---|---|---|
| Nymeria account token | Logging into the web UI/API as a user | `nym_...` |
| Bootstrap token | First login for the auto-created `default` admin | `data/BOOTSTRAP_TOKEN.txt` |
| Internal service token | Worker, watchdog, bots, MCP, and service act-as calls | `NYMERIA_SERVICE_TOKEN`, or `data/SLIM_SERVICE_TOKEN.txt` in slim mode |
| LLM provider key | Talking to Anthropic, OpenAI, OpenRouter, or another model provider | Provider-specific, usually `sk-...`, `sk-ant-...`, or `sk-or-...` |

Do not paste provider keys into the web UI account-token field. If you need
another login token for an existing user, issue a new account token. Rotate only
when the old tokens should stop working.

## Bootstrap

On first run with an empty `users` table, the agent auto-creates a default admin:

- `id`: `default`
- `email`: `owner@localhost`
- `display_name`: `Owner`
- `role`: `admin`

The raw token is:
1. Announced at WARNING level with a loud banner and token-file path
2. Written to `<data_dir>/BOOTSTRAP_TOKEN.txt` (mode 0600)

Paste the `nym_...` token from that file into the Desktop/Mobile Setup Wizard
in place of the old `NYMERIA_API_KEY`. The server deletes the file after the
first successful authentication with that token; deleting it manually after
setup is still fine. Do not paste a provider API key such as an Anthropic,
OpenAI, or OpenRouter key. The `default` user ID lines up with existing
per-user file paths (`data/todos/default.json`, `data/profiles/default.json`,
etc.) so no data migration is needed for the first user.

### Slim-launcher service token (`data/SLIM_SERVICE_TOKEN.txt`)

`python3 run.py slim` provisions a separate `bot-service` admin user and
persists its raw token at `data/SLIM_SERVICE_TOKEN.txt` (mode `0600`). This
token authenticates same-process MCP, watchdog, trigger-fire, and
command-service calls; it is verified against the accounts repo on every
boot and rotated automatically if invalid. It is an **internal service
credential**, not a human bootstrap token - do not paste it into the Setup
Wizard. See [deployment-slim.md](deployment-slim.md) for the full launcher
reference.

## CLI

The `users` CLI operates directly on the accounts DB and does not require a
running API. Prefer the HTTP endpoints for normal account administration, but
keep the CLI for first-boot setup, emergency token rotation, and API-down
maintenance.

```bash
# Create a user, mint a token
python3 run.py users add alice@example.com --role user --id alice --display-name "Alice"
# prints: Token: nym_...   (shown once)

# List users
python3 run.py users list

# Issue an additional token for an existing user without revoking old tokens
python3 run.py users issue-token owner@localhost --label desktop
python3 run.py users issue-token default --label desktop

# Temporarily disable / re-enable
python3 run.py users disable alice@example.com
python3 run.py users enable alice@example.com

# Revoke every active token, then mint a replacement
python3 run.py users rotate-token alice@example.com

# Link a chat-platform identity to a user (enables bot routing)
python3 run.py users link-platform alice@example.com discord 123456789
python3 run.py users platforms alice@example.com
python3 run.py users unlink-platform discord 123456789
```

Inside Docker, run the command in the API container so it uses the live
`/data/accounts.db` database from the Docker volume:

```bash
docker exec nymeria-api python3 run.py users list
docker exec nymeria-api python3 run.py users issue-token default --label desktop
```

## HTTP Admin API

Every account operation is exposed as a REST endpoint, gated by `require_admin_user`. Pass an admin token (the bootstrap token works for first-time setup) as `Authorization: Bearer nym_...`. Raw tokens are returned **once**, in the response body of a creation/issue/rotate call - there is no way to retrieve them later.

### Self (`/me`, `/me/tokens`) - any authenticated user

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/me` | - | Resolve the calling token to its user (`id`, `email`, `display_name`, `role`). |
| `PATCH` | `/me` | `{display_name?}` | Update your own display name. |
| `GET` | `/me/tokens` | - | List your active and revoked tokens (no raw values). |
| `POST` | `/me/tokens` | `{label?}` | Issue yourself a new token. Raw shown ONCE. |
| `DELETE` | `/me/tokens/{token_hash_prefix}` | - | Revoke one of your tokens by hash prefix. |
| `GET` | `/me/platforms` | - | List your linked chat-platform identities. Self-service equivalent of `/admin/users/{user_id}/platforms`. |
| `POST` | `/me/platform-link-codes` | `{provider}` | Issue a short-lived code (8 chars, 10-min TTL) the bot consumes to link your platform identity to your Nymeria account. The response includes a `t.me/<bot>?start=link_<code>` deep link when `TELEGRAM_BOT_USERNAME` is set. |
| `GET` | `/me/telegram-bots` | - | List your BYO Telegram bots (token-paste registrations). No token material returned. |
| `GET` | `/me/telegram-bots/{bot_id}` | - | Single-bot fetch. The wizard polls this after registration to wait for the supervisor's first heartbeat (`last_seen_at` becomes non-null) before showing the bind step. |
| `POST` | `/me/telegram-bots` | `{bot_token}` | Register a BYO bot from a BotFather token. Validates via `getMe`, encrypts with `NYMERIA_SECRETS_KEY`, stores ciphertext. Idempotent: re-pasting the same token returns the existing record. Returns 503 if `NYMERIA_SECRETS_KEY` isn't set. |
| `DELETE` | `/me/telegram-bots/{bot_id}` | - | Remove a BYO bot. Cascades to its bindings; supervisor stops its polling loop on the next refresh. |

### Self thread bindings (`/threads/{thread_id}/chatapp`) - caller must own the thread

| Method | Path | Body | Purpose |
|---|---|---|---|
| `POST` | `/threads/{thread_id}/chatapp/bind-code` | `{provider}` | Issue a single-use code the user types (or deep-links via `/start bind_<code>`) into the bot to attach the chat to this thread. Returns 409 if the thread is already bound. |
| `GET` | `/threads/{thread_id}/chatapp/bindings` | - | List active bindings for the thread (today only one per provider). |
| `DELETE` | `/threads/{thread_id}/chatapp/bindings/{binding_id}` | - | Unbind. |

### Admin chat-app routes (`/admin/chatapp`, `/admin/platform/link-codes`) - bots only

These admin endpoints exist so chat-app bots and webhooks can
consume the codes the desktop wizard mints and look up the routing map
without holding per-user tokens. They're not called by the frontend.
They are rate-limited per admin/service user and endpoint; callers that exceed
the guard receive `429` with a `Retry-After` header. The default limit is high
enough for normal bot startup, binding-cache refreshes, and BYO-bot supervisor
polling, but bounds the impact of a misbehaving bot loop.

| Method | Path | Body / Query | Purpose |
|---|---|---|---|
| `GET` | `/admin/chatapp/bindings` | `?provider=` | Bulk list bindings (filtered by provider). Bot startup uses this to populate its `chat_id <-> thread_id` cache. |
| `GET` | `/admin/chatapp/bindings/lookup` | `?provider=&platform_chat_id=` or `?provider=&thread_id=` | Resolve a single binding for inbound or outbound routing. |
| `POST` | `/admin/chatapp/bindings/claim` | `{code, provider, platform_chat_id, expected_provider_user_id}` | Four-phase claim used by the **shared** bot: inspect the bind code (read-only), verify the platform user matches the issuing Nymeria account, create the binding row, then consume the code. Failed authorization or failed binding creation does not burn the code. |
| `POST` | `/admin/chatapp/bindings/claim-via-bot` | `{code, provider, platform_chat_id, via_user_telegram_bot_id}` | Four-phase claim used by **user-owned** bots: inspect the bind code, verify the issuer matches the bot's `owner_user_id`, create the binding with `user_telegram_bot_id` set, then consume the code. Failed authorization or failed binding creation does not burn the code. |
| `DELETE` | `/admin/chatapp/bindings/by-chat` | `?provider=&platform_chat_id=` | Bot's `/unbind` handler. |
| `POST` | `/admin/platform/link-codes/claim` | `{code, provider, platform_user_id}` | Bot's `/start link_<code>` handler. Four-phase claim: inspect the `platform_link` code, check for existing platform identity conflicts, create the `platform_identities` row, then consume the code. Failed authorization or failed link creation does not burn the code. |
| `GET` | `/admin/telegram-bots` | - | Used by the supervisor in `nymeria-telegram-bot`. Lists all enabled BYO bots **with decrypted tokens**. Returns `[]` when `NYMERIA_SECRETS_KEY` isn't configured. |
| `POST` | `/admin/telegram-bots/{bot_id}/seen` | - | Heartbeat from the supervisor after a successful refresh of this bot's polling loop; updates `last_seen_at`. |

### Admin (`/admin/users`) - caller must be admin

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/admin/users` | - | List every account with role, status, token count, last token use. |
| `POST` | `/admin/users` | `{email, display_name?, role?, id?, token_label?}` | Create a user and mint a first token. Raw shown ONCE. |
| `GET` | `/admin/users/{user_id}` | - | Full user record + thread/todo/platform counts. |
| `PATCH` | `/admin/users/{user_id}` | `{display_name?, role?, disabled?}` | Update fields. Demoting or disabling the only enabled admin returns 409. |
| `DELETE` | `/admin/users/{user_id}` | - | Cascade delete (tokens, platform identities). 409 if user owns threads or todos - clean those up first. |
| `GET` | `/admin/users/{user_id}/tokens` | - | List a user's tokens (no raw). |
| `POST` | `/admin/users/{user_id}/tokens` | `{label?}` | Issue a token for the user. Raw shown ONCE. |
| `POST` | `/admin/users/{user_id}/tokens/rotate` | `{label?}` | Revoke every active token for the user, mint a fresh one. Raw shown ONCE. |
| `DELETE` | `/admin/users/{user_id}/tokens/{token_hash_prefix}` | - | Revoke a single token by hash prefix. |
| `GET` | `/admin/users/{user_id}/platforms` | - | List a user's linked chat-platform identities. |
| `POST` | `/admin/users/{user_id}/platforms` | `{provider, provider_user_id}` | Link a platform identity. 409 if already linked to another user. |
| `DELETE` | `/admin/users/{user_id}/platforms/{provider}/{provider_user_id}` | - | Unlink. |

The `token_hash_prefix` in token revoke paths is the first 8 hex chars of the token's sha256 (returned in the `token_hash_prefix` field of `GET /me/tokens` and the admin token list). Prefixes shorter than 4 chars are rejected; ambiguous prefixes return 400.

### Frontend integration (summary)

Every endpoint above has a wrapper in `nymeria-{desktop,mobile}/src/lib/services/api.svelte.ts`. The wrappers all route their non-2xx responses through a single private `_toastAndExtractError()` helper that pushes a structured toast to the global `errorsStore` queue. Status-code mapping:

| Status | Toast kind | Side effect |
|---|---|---|
| 401 | `auth_invalid` | Auto-signs the user out and clears the active connection so SetupWizard renders. |
| 403 | `forbidden_admin` | Toast only. |
| 409 (last-admin) | `last_admin` | Toast only. |
| 409 (owns threads/todos) | `resource_owned` | Toast only. |
| Other non-2xx | `generic` | Toast only. |

The toast layer (`stores/errors.svelte.ts` + `components/common/ErrorToast.svelte`) is mounted at the app root in `routes/+page.svelte` so it survives modal switching. See [`frontend-accounts.md`](frontend-accounts.md) for the full data flow, component reference, and a debugging table mapping common symptoms to source files.

### Bootstrapping the service token via HTTP

```bash
TOKEN=$(grep -oE 'nym_[A-Za-z0-9_-]+' Nymeria/data/BOOTSTRAP_TOKEN.txt)

# Create the bot service user and capture its raw token
curl -sX POST http://localhost:8000/admin/users \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email":"bot-service@localhost","role":"admin","id":"bot-service","token_label":"service"}' \
  | jq -r .raw_token
```

Paste that into `NYMERIA_SERVICE_TOKEN` in `.env.docker`, then `docker compose up -d` to propagate.

## Service token

Bots, the ticker, and the watchdog call the API as admin with `X-Nymeria-Act-As: <user_id>` to route per-user traffic without holding each user's raw token.

**Create the service user once** (on a fresh install):

```bash
docker exec nymeria-api python3 run.py users add bot-service@localhost --role admin --id bot-service
```

Copy the printed token into `.env.docker`:

```
NYMERIA_SERVICE_TOKEN=nym_...
```

Then `docker compose --env-file .env.docker up -d` to propagate the variable into every container. Discord/Telegram/Watchdog print `Auth: service token` at startup when they pick it up - `NYMERIA_API_KEY` is retired, so the service token is now the only way for shared infrastructure to authenticate.

**How the header is honored:** Every authenticated route now resolves the caller via `verify_api_key`/`require_user`, which honors `X-Nymeria-Act-As: <user_id>` for admin callers. Non-admin callers sending it get 403; unknown/disabled targets get 404. Bots and the watchdog rely on this everywhere - they hold the admin service token and act-as the resolved per-user identity per request.

**What the bots send:**
```
Authorization: Bearer <NYMERIA_SERVICE_TOKEN>
X-Nymeria-Act-As: <resolved_user_id>
```
where `<resolved_user_id>` is the Nymeria account the platform user maps to via `platform_identities`.

## Storage location

- **Local:** `Nymeria/data/accounts.db` (override with `NYMERIA_DATA_DIR`)
- **Docker:** `nymeria_data:/data/accounts.db` (the `nymeria_data` named volume)

For a Docker stack, `python3 run.py users ...` on the host may point at
`Nymeria/data/accounts.db`, which is not necessarily the database the running
API container is using. Prefer `docker exec nymeria-api python3 run.py users ...`
when administering the live stack.

Back this file up alongside `nymeria.db` - losing it locks every user out.

## Per-user OAuth token caches

Google (Calendar/Docs/Drive/Sheets/Gmail) and Microsoft (Outlook/Graph) OAuth tokens live at `data/auth_tokens/<user_id>/` - isolated per Nymeria user so one account's bot agent can't read another's Gmail/Outlook.

| Service | Cache file |
|---|---|
| Google Calendar | `data/auth_tokens/<user_id>/google_calendar.json` |
| Google Docs/Drive/Sheets | `data/auth_tokens/<user_id>/google_docs.json` |
| Google Gmail | `data/auth_tokens/<user_id>/google_gmail.json` |
| Gmail MCP credential export | `data/auth_tokens/<user_id>/mcp/gmail/credentials.json` |
| Microsoft Outlook/Graph | `data/auth_tokens/<user_id>/microsoft.json` |

**On first boot with the per-user refactor**, existing global caches (`data/auth_tokens/.microsoft_mcp_token_cache.json` etc.) are automatically migrated to `data/auth_tokens/default/*.json`. The owner's existing Google/Outlook auth survives - other users start with empty caches and connect their own accounts via the unified `request_credential(provider=..., kind="oauth")` flow (one descriptor per provider; see `nymeria/config/oauth_providers.py`).

Tools resolve the current caller's `user_id` via `RunnableConfig` injection (the agent sets `configurable.user_id` on every graph invocation). No tool can be tricked into loading a different user's token cache.

If an OAuth token becomes stale, revoked, or attached to the wrong account, the user removes the credential from Settings > Connections or the agent calls `auth_cleanup(operation="disable", credential_id=...)`. For `google_gmail` specifically, the descriptor's `post_clear_hook` also removes the exported MCP credentials file.

Tokens land in the encrypted credential vault as `kind=oauth_token` when `NYMERIA_SECRETS_KEY` is configured. Token reads do a vault-first lookup with a read-only fallback to the legacy file caches; production installs should use the vault path. File fallback storage creates `data/auth_tokens/<user_id>/` with `0700` permissions and cache files with `0600` permissions.

Google OAuth start flows use PKCE (`S256`). Per-user Google token caches store the user's access/refresh token and OAuth `client_id`, but do not store the app-wide `client_secret`; refresh resolves that secret from the operator-provided `GOOGLE_OAUTH_CREDENTIALS` file.

## Thread ownership

`thread_owners` enforces "who can read/mutate this thread" for normal users.
`GET /threads` returns the effective user's owned threads plus safe recovery
rows; pass `owned_only=true` when tooling needs only explicit owner rows.
The desktop client uses `GET /threads?owned_only=true` for its sidebar so an
admin token signed in as itself still sees only admin-owned threads, not other
users' owned threads or orphan recovery rows.
There is no universal "all threads" list view. Admin callers bypass
thread-owner checks on direct thread routes if they already know the thread ID,
and should use `X-Nymeria-Act-As: <user>` when they want user-scoped metadata,
TODOs, triggers, chat bindings, and deletion behavior to line up with the
target owner.

### Lifecycle

A thread becomes owned in one of four ways:

1. **Eager claim from first-party clients.** `threadsStore.createThread()` (`nymeria-desktop/src/lib/stores/threads.svelte.ts`) generates a UUID and immediately fires `POST /threads/{thread_id}/claim` (fire-and-forget) so the backend has an ownership row before any chat-app binding can route a message into the thread. The CLI also uses this endpoint for CLI-generated startup threads and `/thread new`, passing `platform: "cli"` so the shared thread list can distinguish CLI-created threads from desktop-created threads. Without an eager claim, an admin who created a UUID, bound it to a Telegram chat, and let the bound user send the first message would silently transfer ownership to that user.
2. **Admin first-touch on `/chat`.** `_require_thread_access` (`triggers/api.py`) claims personal-pattern threads on first touch when `user.role == "admin"`. Bot service token + missing `act_as` lands here too, which is fine - the thread becomes service-owned rather than ownerless.
3. **Non-admin first-touch on `/chat`.** TOFU via `claim_thread` (`core/accounts.py`, `INSERT OR IGNORE`, race-safe). Subsequent access by any other non-admin user resolves to 404.
4. **Startup orphan-backfill.** `NymeriaAgent.__init__` enumerates checkpoint thread_ids, filters to personal patterns (NOT shared-channel), and assigns any without a `thread_owners` row to the bootstrap admin. Idempotent across restarts.

### Personal vs shared patterns

`_is_shared_channel_thread` (`triggers/api.py`) classifies thread IDs:

| Pattern | Type | Claimable per-user? |
|---|---|---|
| UUIDv4 (`abc-123-...`), `agent-*`, `spawned-*`, plain strings | Personal | Yes |
| `discord_dm_<channel_id>` | Personal Discord DM | Yes |
| `telegram_<positive_id>` | Personal Telegram DM | Yes |
| `slack_dm_<team>_<user>` | Personal Slack DM | Yes |
| `whatsapp_<sender>` | Personal WhatsApp customer chat | Yes |
| `teams_dm_<tenant>_<user>` | Personal Teams DM | Yes |
| `discord_<guild_id>_<channel_id>` | Shared Discord guild channel | **No** (400 from `/claim`) |
| `telegram_-<group_id>` | Shared Telegram group/supergroup | **No** |
| `slack_<team>_<channel>` | Shared Slack channel/group DM | **No** |
| `whatsapp_group_<id>` | Shared WhatsApp group | **No** |
| `teams_<tenant>_<conversation>` | Shared Teams chat/channel | **No** |

Shared-channel threads are inherently multi-user - per-user ownership rows would just claim-jack to whichever user spoke first. The bot service token routes per-user attribution through `X-Nymeria-Act-As` instead (act-as targets arrive with `role="user"` plus `via_act_as=true` and bypass shared-channel owner claiming). Direct non-admin API calls to a shared-channel thread without admin act-as return 404.

### Operator notes

- A thread can be reassigned by direct DB write: `UPDATE thread_owners SET user_id=<new> WHERE thread_id=<id>`. The checkpoint-metadata `user_id` field on existing messages is **not** rewritten - it reflects who was authenticated when each message was sent. Future messages will attribute to the new owner.
- Deleting a thread (`DELETE /threads/{thread_id}`) cascades through `thread_owners`, checkpoints, metadata, and config.
- The orphan-backfill sweep on every restart is cheap and idempotent. If you don't want it (e.g. you intentionally hold ownerless threads), comment out the second pass in `NymeriaAgent.__init__`.

## Planned: Optional Account Passwords and Email Reset

The CLI (`python3 run.py users issue-token` and `rotate-token`) operates
directly on SQLite with no authentication. On a single-operator VPS this is
fine - CLI access implies SSH access. On shared or bare-metal production
installs (e.g. a Windows service), any local user could run the CLI to mint
themselves an admin token.

The `password_hash` column already exists in the `users` table but is not
wired up. The plan:

1. **Optional per-user password.** If a user sets a password (via UI or API),
   CLI token-issuing commands and API `POST /me/tokens` require it. If unset,
   current token-only behavior is unchanged.
2. **Email reset flow.** A "forgot password" path sends a time-limited reset
   link to the user's email, covering the lockout scenario without needing
   the CLI escape hatch.
3. **`REQUIRE_ACCOUNT_PASSWORD` setting.** When `true`, all users must set a
   password. Production admins enable this; solo/dev installs leave it off
   (default `false`).

This keeps single-operator setups frictionless while letting production
deployments enforce password auth on token operations. See also the
corresponding note in [`credentials.md`](credentials.md).

## Rationale for dedicated SQLite

Accounts live in their own file rather than co-located with LangGraph's checkpoint database. The checkpoint backend is backend-switchable (SQLite locally, Postgres in Docker); keeping accounts SQLite-only keeps the account-layer code single-backend and avoids forcing a `psycopg` dependency path for what is fundamentally a low-volume, latency-insensitive store. The legacy thread backfill reads `DISTINCT thread_id FROM checkpoints` and writes to `thread_owners` - two connections, one-shot, trivial.
