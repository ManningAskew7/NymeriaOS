# Nymeria REST API

Base URL: `http://localhost:8000`

Implementation note: `create_api_app()` remains the public FastAPI factory.
The API is being split incrementally; the System slice (`/health`, `/ready`,
`/restart`, `/report`), device, workspace, RAG, user memory, user
tool-preference, Skills, voice, Agent Threads, activity/notification, TODO
dashboard, autonomous stream, custom tools, classic tool discovery/default/
callable routes, unified tools, MCP server management, settings/model catalog,
thread config/callable-team routes, chat-app/BYO Telegram routes, command
routes, and Chat SSE routes now live under `Nymeria/nymeria/api/routers/`,
while the rest of the
surface still lives in `Nymeria/nymeria/triggers/api.py` during the migration.

## Authentication

Most endpoints require Bearer token authentication:

```
Authorization: Bearer <token>
```

Per-user account tokens (`nym_<32-url-safe>`) are the only accepted bearer. Created via `python run.py users add` — see `docs/accounts.md`. Resolve to the user they were issued to.

`X-Nymeria-Act-As: <user_id>` is honored only for admin-role callers and rewrites the effective user to the target (403 for non-admin, 404 for unknown/disabled target).

Failed bearer authentication attempts are rate-limited per client IP. After 10 missing, malformed, or invalid tokens within 60 seconds, further failed attempts return `429` with a `Retry-After` header. Valid tokens are not blocked by this failed-auth counter.

Every API response includes `X-Request-ID`. If the client sends a safe
`X-Request-ID` header, Nymeria propagates it; otherwise Nymeria generates one
and includes it in request-scoped logs.

Exceptions without Bearer auth:
- `GET /health`
- `GET /ready`
- `POST /triggers/fire/{trigger_id}` (public callers must provide the trigger's shared `secret`; Bearer auth can be used instead)

---

## Endpoints

### Health Check

```http
GET /health
```

No authentication required.

**Response:**
```json
{
  "status": "ok",
  "version": "<nymeria.__version__>"
}
```

---

### Readiness Check

```http
GET /ready
```

No authentication required. Checks the configured database backend and Redis
when Redis is enabled. Results are cached for 1 second.

Returns `200` when all required dependencies are ready and `503` when any
required dependency fails.

**Response:**
```json
{
  "status": "ok",
  "version": "<nymeria.__version__>",
  "checks": {
    "database": {"status": "ok", "detail": "sqlite"},
    "redis": {"status": "skipped", "detail": "disabled"}
  }
}
```

---

### Who Am I

```http
GET /me
Authorization: Bearer <token>
```

Returns the account identity the token resolves to. Used by frontends to
discover their own `user_id` for localStorage namespacing. Requires a per-user
account token (`nym_...`); the legacy `NYMERIA_API_KEY` was retired in Step 3c.
Admins can pass `X-Nymeria-Act-As: <user_id>` to read another user's identity.

**Response:**
```json
{
  "id": "default",
  "email": "owner@localhost",
  "display_name": "Owner",
  "role": "admin"
}
```

Returns 401 for missing/invalid/revoked tokens.

---

### Resolve Platform Identity

```http
GET /platform/resolve?provider=<provider>&provider_user_id=<id>
Authorization: Bearer <admin-token>
```

Admin-only. Resolves a chat-platform user ID to its linked Nymeria `user_id`
via the `platform_identities` table. Used by bot clients together with
`X-Nymeria-Act-As` to route per-user traffic without holding raw per-user
tokens.

| Field | Values |
|---|---|
| `provider` | `discord`, `telegram`, `twitch`, `slack`, `matrix`, `whatsapp`, `messenger`, `instagram`, `webex`, `mattermost`, `zulip`, `rocketchat`, `teams`, `googlechat`, `line`, `signal` |
| `provider_user_id` | Platform-native user ID (string) |

**Responses:**
- `200` — `{"user_id": "bob"}`
- `400` — `{"detail": "Unknown provider"}`
- `403` — `{"detail": "Admin only"}` (non-admin token)
- `404` — `{"detail": "Not linked"}`

Create mappings via `POST /admin/users/{id}/platforms` (see Account & User Administration below).

---

### Credential Vault

```http
GET /credentials?scope=visible|mine|system|all
POST /credentials
GET /credentials/{credential_id}
PATCH /credentials/{credential_id}
DELETE /credentials/{credential_id}
POST /credentials/{credential_id}/test
GET /credentials/{credential_id}/bindings
POST /credentials/{credential_id}/bindings
DELETE /credential-bindings/{binding_id}
POST /credential-setup-sessions
Authorization: Bearer <token>
```

Stores reusable Nymeria tool/MCP/custom HTTP credentials encrypted with
`NYMERIA_SECRETS_KEY`. Responses return metadata, allowed targets, scopes,
status, and secret field names only. Plaintext is accepted only in write-only
`secret_fields` request properties and is never echoed.

Credential references use `${credential:<credential_id>.<field>}` in runtime
config. The backend resolves them inside MCP/custom HTTP execution after target
scope checks.

See [`credentials.md`](credentials.md) for the storage model and migration notes.

---

### Account & User Administration

Self endpoints (`/me/*`) work for any authenticated user. Admin endpoints (`/admin/users/*`) require an admin token. Raw tokens are returned **once** in the response of any creation/issue/rotate call — they cannot be retrieved later.

Token revoke endpoints address tokens by `token_hash_prefix` (the first 8 hex chars of the sha256, returned in token list responses). The frontend never sees raw token material for tokens it didn't just mint.

**Self — any authenticated user:**

| Method | Path | Body | Notes |
|---|---|---|---|
| `PATCH` | `/me` | `{display_name?}` | Update your own display name. Empty value → 400. |
| `GET` | `/me/tokens` | — | List your tokens (no raw values). |
| `POST` | `/me/tokens` | `{label?}` | Issue yourself a token. Returns `{raw_token, metadata}`. |
| `DELETE` | `/me/tokens/{prefix}` | — | Revoke. 400 on ambiguous prefix, 404 on no match. |

**Admin — caller must be admin:**

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/admin/users` | — | List every user with `token_count` and `last_token_use`. |
| `POST` | `/admin/users` | `{email, display_name?, role?, id?, token_label?}` | Create + issue first token. Returns `IssuedTokenResponse`. |
| `GET` | `/admin/users/{id}` | — | Single user with `thread_count`, `todo_count`, `platform_count`. |
| `PATCH` | `/admin/users/{id}` | `{display_name?, role?, disabled?}` | 409 if it would leave zero enabled admins. |
| `DELETE` | `/admin/users/{id}` | — | 409 if user owns threads or todos; clean those first. |
| `GET` | `/admin/users/{id}/tokens` | — | List a user's tokens. |
| `POST` | `/admin/users/{id}/tokens` | `{label?}` | Issue a token for the user. |
| `POST` | `/admin/users/{id}/tokens/rotate` | `{label?}` | Revoke all + issue one. Returns `RotatedTokensResponse` with `revoked_count`. |
| `DELETE` | `/admin/users/{id}/tokens/{prefix}` | — | Revoke single by hash prefix. |
| `GET` | `/admin/users/{id}/platforms` | — | List chat-platform identities. |
| `POST` | `/admin/users/{id}/platforms` | `{provider, provider_user_id}` | Link. 409 if already owned by another user. |
| `DELETE` | `/admin/users/{id}/platforms/{provider}/{provider_user_id}` | — | Unlink. |

**Response shapes** (Pydantic models for this account surface live in
`Nymeria/nymeria/api/schemas/accounts.py`; other extracted schemas such as
System, Settings, Skills, Agent Threads, TODOs, MCP servers, Chat App/BYO
Telegram, and dashboard activity/notifications live under
`Nymeria/nymeria/api/schemas/`):

```jsonc
// IssuedTokenResponse — returned by POST /me/tokens, POST /admin/users,
// POST /admin/users/{id}/tokens. Raw token shown ONCE; the metadata.token_hash_prefix
// is the stable handle for revocation.
{
  "raw_token": "nym_...",
  "metadata": {
    "token_hash_prefix": "a3f9b1c2",
    "label": "initial",
    "created_at": "2026-04-25T10:00:00+00:00",
    "last_used_at": null,
    "revoked_at": null
  }
}

// RotatedTokensResponse — returned by POST /admin/users/{id}/tokens/rotate.
// Same as IssuedTokenResponse plus the count of tokens that were just revoked.
{
  "raw_token": "nym_...",
  "metadata": { /* TokenInfo */ },
  "revoked_count": 3
}

// TokenInfo — returned by GET /me/tokens and GET /admin/users/{id}/tokens.
// No raw_token field — that's only ever in the issue/rotate response.
{
  "token_hash_prefix": "a3f9b1c2",
  "label": "iPhone",
  "created_at": "2026-04-25T10:00:00+00:00",
  "last_used_at": "2026-04-25T11:42:13+00:00",
  "revoked_at": null
}

// AdminUserResponse — returned by GET /admin/users (as a list) and
// GET /admin/users/{id}. The thread/todo/platform counts are present on
// the single-user GET; null in the list response (kept in sync with the
// frontend's `thread_count?: number` shape).
{
  "id": "default",
  "email": "owner@localhost",
  "display_name": "Owner",
  "role": "admin",
  "disabled": false,
  "created_at": "2026-04-24T12:07:30+00:00",
  "updated_at": "2026-04-25T10:56:43+00:00",
  "token_count": 1,
  "last_token_use": "2026-04-25T13:52:39+00:00",
  "thread_count": 12,
  "todo_count": 3,
  "platform_count": 2
}

// PlatformIdentityResponse — returned by GET /admin/users/{id}/platforms (as a list)
// and POST /admin/users/{id}/platforms (single).
{
  "provider": "discord",
  "provider_user_id": "699436710118817823",
  "created_at": "2026-04-24T13:34:05+00:00"
}
```

**Common errors mapped to UI behaviour:**

| Status | Body | UI behaviour |
|---|---|---|
| `401` | `{"detail": "Invalid or revoked token"}` | Frontend `_toastAndExtractError` triggers `pushAuthInvalid` → user signed out, routed to SetupWizard. |
| `429` | `{"detail": "Too many failed authentication attempts"}` | Retry after the `Retry-After` header duration. |
| `403` | `{"detail": "Admin role required"}` | Toast: "Admin role required". |
| `409` | `{"detail": "Cannot demote the only enabled admin"}` | Toast: `last_admin` kind. |
| `409` | `{"detail": "User still owns N threads / M todos"}` | Toast: `resource_owned` kind. |
| `400` | `{"detail": "Token prefix must be at least 4 chars"}` | Generic toast. |
| `400` | `{"detail": "Ambiguous token prefix matches N tokens"}` | Generic toast. |

**curl examples:**

```bash
TOKEN=$(grep -oE 'nym_[A-Za-z0-9_-]+' Nymeria/data/BOOTSTRAP_TOKEN.txt)

# Create a non-admin user and capture their first token
curl -sX POST http://localhost:8000/admin/users \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email":"alice@example.com","role":"user","token_label":"initial"}' \
  | jq

# List all users + token counts
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/admin/users | jq

# Issue a fresh personal token labelled "iPhone"
curl -sX POST http://localhost:8000/me/tokens \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"label":"iPhone"}' | jq -r .raw_token

# Rotate every active token for alice (revokes all + mints a new one)
curl -sX POST "http://localhost:8000/admin/users/alice/tokens/rotate" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"label":"post-rotate"}' | jq

# Link a Discord ID to alice
curl -sX POST "http://localhost:8000/admin/users/alice/platforms" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"provider":"discord","provider_user_id":"123456789"}' | jq

# Try to demote the last enabled admin (returns 409)
curl -i -X PATCH "http://localhost:8000/admin/users/default" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"role":"user"}'
```

See [`accounts.md`](accounts.md) for the data model, bootstrap admin flow, and service-token recipe. See [`frontend-accounts.md`](frontend-accounts.md) for how each endpoint is wrapped on the client side and how errors map to toasts.

---

### Chat App Bindings And BYO Telegram Bots

These routes live in `Nymeria/nymeria/api/routers/chat_apps.py`; schemas live
in `Nymeria/nymeria/api/schemas/chat_apps.py`. The desktop/mobile Chat App
wizard uses the self-service routes. Shared bot/webhook clients use the
admin/service-token routes, which are rate-limited per admin token and endpoint.

**Self-service — any authenticated user:**

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/me/platforms` | — | List linked chat-platform identities for the caller. |
| `POST` | `/me/platform-link-codes` | `{provider:"telegram\|slack\|matrix\|whatsapp\|messenger\|instagram\|webex\|mattermost\|zulip\|rocketchat\|teams\|googlechat\|line\|signal"}` | Issue a 10-minute self-link code. Telegram responses include an optional `t.me` deep link; shared bot providers use the raw `link <code>` command. |
| `POST` | `/threads/{thread_id}/chatapp/bind-code` | `{provider:"telegram\|slack\|matrix\|whatsapp\|messenger\|instagram\|webex\|mattermost\|zulip\|rocketchat\|teams\|googlechat\|line\|signal"}` | Issue a 10-minute thread-bind code. Caller must own the thread and the thread must not already be bound. |
| `GET` | `/threads/{thread_id}/chatapp/bindings` | — | List chat-app bindings for a thread the caller owns. |
| `DELETE` | `/threads/{thread_id}/chatapp/bindings/{binding_id}` | — | Delete a binding owned by the caller and emit sidebar platform sync. |
| `GET` | `/me/telegram-bots` | — | List user-owned Telegram bots without token material. |
| `POST` | `/me/telegram-bots` | `{bot_token}` | Validate via Telegram `getMe`, encrypt with `NYMERIA_SECRETS_KEY`, and register idempotently for the same owner. |
| `GET` | `/me/telegram-bots/{bot_id}` | — | Fetch one owned bot; used by the wizard while waiting for supervisor heartbeat. |
| `DELETE` | `/me/telegram-bots/{bot_id}` | — | Delete an owned bot and cascade-delete bindings served by that bot. |

**Bot/admin — caller must be admin:**

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/admin/chatapp/bindings` | — | List all bindings, optionally filtered by `?provider=telegram`, `slack`, `matrix`, `whatsapp`, `messenger`, `instagram`, `webex`, `mattermost`, `zulip`, `rocketchat`, `teams`, `googlechat`, `line`, or `signal`. |
| `GET` | `/admin/chatapp/bindings/lookup` | — | Resolve by exactly one of `platform_chat_id` or `thread_id`. |
| `POST` | `/admin/chatapp/bindings/claim` | `{code, provider, platform_chat_id, expected_provider_user_id}` | Shared-bot bind-code claim; verifies the platform user is linked to the issuing Nymeria user before consuming the code. |
| `POST` | `/admin/chatapp/bindings/claim-via-bot` | `{code, provider, platform_chat_id, via_user_telegram_bot_id}` | User-owned bot bind-code claim; authorizes by bot owner instead of platform identity. |
| `DELETE` | `/admin/chatapp/bindings/by-chat` | — | Remove a binding by `provider` and `platform_chat_id`; optional `user_id` and `user_telegram_bot_id` query params scope bot-initiated deletes to the expected owner/bot. |
| `POST` | `/admin/chatapp/bindings/switch` | `{provider, platform_chat_id, thread_id, user_id, user_telegram_bot_id?}` | Move a chat-app binding to another existing user-owned non-native thread. |
| `POST` | `/admin/platform/link-codes/claim` | `{code, provider, platform_user_id}` | Consume a self-link code and create the platform identity row. |
| `GET` | `/admin/telegram-bots` | — | Supervisor-only list of enabled user-owned bots with decrypted tokens. |
| `POST` | `/admin/telegram-bots/{bot_id}/seen` | — | Supervisor heartbeat update for `last_seen_at`. |

Bind-code claim routes inspect and authorize before consuming a code. If
authorization or binding creation fails, the code remains reusable until it
expires; successful claims consume it.

See [`telegram-bot.md`](telegram-bot.md), [`slack-bot.md`](slack-bot.md),
[`matrix-bot.md`](matrix-bot.md), [`mattermost-bot.md`](mattermost-bot.md),
[`zulip-bot.md`](zulip-bot.md), [`rocketchat-bot.md`](rocketchat-bot.md), [`signal-bot.md`](signal-bot.md),
[`whatsapp-bot.md`](whatsapp-bot.md), [`messenger-bot.md`](messenger-bot.md), [`instagram-bot.md`](instagram-bot.md), [`webex-bot.md`](webex-bot.md),
[`teams-bot.md`](teams-bot.md), [`google-chat-bot.md`](google-chat-bot.md), and
[`line-bot.md`](line-bot.md)
for client-specific commands and setup behavior.

**WhatsApp Cloud API webhook:**

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/integrations/whatsapp/webhook` | Meta challenge token | Verifies Meta webhook setup with `hub.mode`, `hub.verify_token`, and `hub.challenge`; `hub.verify_token` must match `WHATSAPP_WEBHOOK_VERIFY_TOKEN`. |
| `POST` | `/integrations/whatsapp/webhook` | Optional Meta signature | Accepts WhatsApp Cloud API message payloads. When `WHATSAPP_APP_SECRET` is set, validates `X-Hub-Signature-256`, then processes messages in the background and replies through the Graph API. |

**Messenger Platform webhook:**

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/integrations/messenger/webhook` | Meta challenge token | Verifies Messenger webhook setup with `hub.mode`, `hub.verify_token`, and `hub.challenge`; `hub.verify_token` must match `MESSENGER_WEBHOOK_VERIFY_TOKEN`. |
| `POST` | `/integrations/messenger/webhook` | Optional Meta signature | Accepts Messenger Page webhook message and postback payloads. When `MESSENGER_APP_SECRET` is set, validates `X-Hub-Signature-256`, then processes messages in the background and replies through the Messenger Send API. |

**Instagram Messaging webhook:**

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/integrations/instagram/webhook` | Meta challenge token | Verifies Instagram webhook setup with `hub.mode`, `hub.verify_token`, and `hub.challenge`; `hub.verify_token` must match `INSTAGRAM_WEBHOOK_VERIFY_TOKEN`. |
| `POST` | `/integrations/instagram/webhook` | Optional Meta signature | Accepts Instagram Messaging webhook message and postback payloads. When `INSTAGRAM_APP_SECRET` is set, validates `X-Hub-Signature-256`, then processes messages in the background and replies through the Instagram Messaging API. |

**Webex Messaging webhook:**

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/integrations/webex/webhook` | Optional Webex signature | Accepts Webex `messages.created` payloads. When `WEBEX_WEBHOOK_SECRET` is set, validates `X-Spark-Signature`, then fetches message details in the background and replies through the Webex Messages API. |

**Microsoft Teams Bot Framework webhook:**

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/integrations/teams/webhook` | Bot Framework bearer token | Accepts Teams Bot Framework `message` activities. When `TEAMS_BOT_VALIDATE_AUTH=true`, validates the connector JWT, then processes messages in the background and replies through the Bot Connector REST API. |

**Google Chat webhook:**

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/integrations/google-chat/webhook` | Google Chat bearer token | Accepts Google Chat `MESSAGE` interaction events. When `GOOGLE_CHAT_VALIDATE_AUTH=true`, validates the Google Chat bearer token, then processes messages in the background and replies through the Google Chat REST API. |

**LINE Messaging API webhook:**

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/integrations/line/webhook` | `x-line-signature` HMAC | Accepts LINE Messaging API webhook events. When `LINE_VALIDATE_SIGNATURE=true`, validates the raw-body HMAC signature, then processes text messages in the background and replies through the LINE push-message API. |

---

### Chat (Streaming)

```http
POST /chat
Content-Type: application/json
Authorization: Bearer <token>
X-Nymeria-Client-Id: <per-client-uuid>
```

`X-Nymeria-Client-Id` is optional but should be sent by desktop/mobile clients.
It is copied onto cross-client sync events so that `GET /autonomous/stream`
subscribers using the same `client_id` can suppress their own echoes.

**Request Body:**
```json
{
  "message": "Hello",
  "thread_id": "optional-thread-id",
  "user_id": "default",
  "attachments": [],
  "force_unsupported_attachments": false,
  "is_self_invoke": false
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `message` | string | Yes | - | User message |
| `thread_id` | string | No | auto-generated | Conversation thread ID |
| `user_id` | string | No | `"default"` | User ID for profile/memory isolation |
| `attachments` | array | No | - | Optional multimodal attachments (images/documents) |
| `force_unsupported_attachments` | bool | No | `false` | Send request even if model modality checks fail |
| `is_self_invoke` | bool | No | `false` | Mark invocation as autonomous/internal. Skips the `message_added` sync event, routes the request through the autonomous prompt path, and mirrors supported stream events (`task_started`, `tool_call_delta`, `tool_call`, `tool_result`, `workspace_artifact`, `tool_reload`, `thinking`, `response`, `context_attached`, `compacting`, `compacted`, `iteration_limit`, `task_completed`) to the autonomous event bus (visible via `GET /autonomous/stream`). Used by the watchdog worker; gated by the same Bearer-auth check as any `/chat` call. |
| `trigger_override` | string | No | - | Label for autonomous invocations (e.g. `"watchdog"`, `"ticker"`). Becomes part of `task_id` and the `source` field on emitted autonomous events. |
| `trigger_id` | string | No | - | Trigger row ID for `trigger_override=="trigger"` calls. Surfaced on `task_started` and `task_completed` for frontend/bot classification. |
| `trigger_name` | string | No | - | Human-readable trigger name for `trigger_override=="trigger"` calls. Surfaced on `task_started` and `task_completed`. |

Messages can start with a thread mention to route the turn to another thread:
`@ThreadName prompt`, `@thread-id-prefix prompt`, or `@"Thread With Spaces" prompt`.
The backend resolves the mention against threads visible to the authenticated
user by metadata title, callable name, or ID prefix, strips the mention before
invoking the agent, and persists the user message plus assistant response only
in the target thread. If the mention does not match any thread, the message is
treated as normal chat. If it matches multiple threads, `/chat` emits an `error`
event with
`code: "mention_ambiguous"` and candidate thread IDs.

**Autonomous caller contract:** Trusted workers and thin clients start internal
work by calling `POST /chat` with `is_self_invoke=true`, a normal Bearer token,
and usually `X-Nymeria-Act-As: <target_user_id>` when using an admin service
token. The direct `/chat` response still streams SSE back to the caller, but the
message is stored as internal autonomous work rather than as a user-authored chat
message.

For self-invoke calls, the API publishes `task_started` to
`/autonomous/stream` only after the first non-`queued` agent chunk. This avoids
moving subscribed clients into autonomous-streaming state while the run is still
waiting on the per-thread lock behind an active user chat. Supported agent
chunks are then mirrored live, and `task_completed` is published when the run
finishes or errors.

**Response:** Server-Sent Events (SSE)

```
data: {"type": "thinking", "content": "...", "thread_id": "abc123"}
data: {"type": "response", "content": "I'll check that now.", "thread_id": "abc123"}
data: {"type": "tool_call_delta", "thread_id": "abc123"}
data: {"type": "tool_call", "name": "web_search", "args": {...}, "thread_id": "abc123"}
data: {"type": "tool_result", "name": "web_search", "result": "...", "thread_id": "abc123"}
data: {"type": "workspace_artifact", "tool_call_id": "tool1", "tool_name": "file_write", "path": "/workspace/report.csv", "name": "report.csv", "mime_type": "text/csv", "size_bytes": 1024, "thread_id": "abc123"}
data: {"type": "response", "content": "...", "thread_id": "abc123"}
data: {"type": "done", "thread_id": "abc123"}
```

Dispatched `@thread` messages first emit:

```
data: {"type": "dispatched", "thread_id": "caller123", "target_thread_id": "target456", "title": "Research", "dispatched_to": {"thread_id": "target456", "title": "Research", "original_thread_id": "caller123"}}
```

Subsequent stream events keep `thread_id` set to the caller thread for client
rendering and include `dispatched_to` metadata for the target. Clients use this
metadata to show a `Response from <thread>` reference line inside the same
assistant turn, not as a separate system message. The rest of the stream remains
normal: `thinking`, pre-tool `response` preamble, `tool_call`, `tool_result`,
post-tool `response`, artifacts, errors, and `done` all flow through the same
rendering pipeline as a non-dispatched chat turn. The target thread owns the
persisted checkpoint history.

---

### Chat (Synchronous)

```http
POST /chat/sync
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** Same as the streaming endpoint, including the optional `is_self_invoke` and `trigger_override` fields. Self-invoke behavior here is identical to `/chat` — it's a pass-through to `agent.chat()` with those flags.

**Response:**
```json
{
  "response": "Hello! How can I help?",
  "thread_id": "abc123"
}
```

---

### Get Thread Status

```http
GET /threads/{thread_id}/status
Authorization: Bearer <token>
```

Lightweight status for sync polling. `revision` is the latest checkpoint ID as
an opaque string, or `null` when the thread has no checkpoint yet. SQL-backed
deployments query checkpoint metadata only and do not deserialize checkpoint
blobs. `processing` is true while the thread has an active agent lock.

The desktop client uses this endpoint for 5-second cross-client sync polling
and only refreshes full history/context when the revision changes, processing
finishes, or it has no local revision baseline. Mobile remains primarily
event/reconnect driven.

**Response:**
```json
{
  "thread_id": "abc123",
  "revision": "1f07bcb2-1d7a-67d2-8003-65db7b8e71f9",
  "processing": false
}
```

---

### Get Thread Overview

```http
GET /threads/{thread_id}/overview
Authorization: Bearer <token>
```

Resolved read model for CLI headers and dashboards. This endpoint is
read-only and does not replace `/threads/{thread_id}/config`: `/config` remains
the editable per-thread config payload, while `/overview` combines the
effective runtime state and counts that previously required many calls.

Top-level sections:

| Section | Contents |
|---------|----------|
| `thread` | id/title/platform/pin/callable/recovery metadata |
| `status` | processing flag and checkpoint revision |
| `context` | token counts, context limit, compaction stats, context mode |
| `config_summary` | customization flags and prompt char counts, not prompt bodies |
| `llm` | effective provider/model/API mode/thinking labels and secret-safe override flags |
| `callable` | callable settings and visible callable-thread tools |
| `tools` | default, disabled, enabled, temporary, MCP, callable, and effective counts |
| `mcp` | server/tool counts and per-server active/discovered counts |
| `skills` | installed/global/thread/active skill and Skill Kit summaries |
| `todos` | per-thread TODO counts, next scheduled item, first labels |
| `triggers` | per-thread trigger counts, health, and fire metadata |
| `chat_apps` | bindings, providers, shared-vs-BYO Telegram bot info, delivery modes |
| `user` | effective authenticated user id/display name/role |
| `section_errors` | non-fatal section failures keyed by section name |

Raw API keys and full instruction/system-prompt bodies are never returned.

---

### Get Thread History

```http
GET /threads/{thread_id}/history
Authorization: Bearer <token>
```

Optional query: `include_internal=true` returns system-generated messages (autonomous wake-ups and compact prompts) that are hidden by default. Compaction markers are visible by default as `system` messages with `kind: "compaction_notice"`.

**Response:**
```json
{
  "thread_id": "abc123",
  "messages": [
    {"id": "abc123-1", "role": "user", "content": "Hello"},
    {
      "id": "abc123-2",
      "role": "assistant",
      "content": "Hi there!",
      "steps": [
        {"type": "thinking", "content": "Reasoning summary..."},
        {"type": "response", "content": "Hi there!"}
      ],
      "intermediate_content": "Reasoning summary..."
    },
    {
      "id": "abc123-3",
      "role": "system",
      "kind": "compaction_notice",
      "content": "Context compacted",
      "context_summary": "Summary of prior work...",
      "messages_removed": 42,
      "auto_resumed": true
    }
  ]
}
```

Assistant `steps` are optional. They appear when a turn has reasoning/thinking, tool calls, or interleaved response chunks. Anthropic typed thinking blocks, OpenAI-compatible `reasoning_content` metadata, and OpenAI Responses `reasoning` summary blocks are returned as `{"type": "thinking"}` steps so desktop and mobile can re-render the same thinking dropdown after history sync. Visible assistant commentary before a tool call is not thinking; it is stored and served as a `{"type": "response"}` step before the `tool_call` step.

**Performance note:** latency scales with the checkpoint count for the thread. Compaction prunes pre-compact rows so healthy threads stay under ~100 ms. If you see multi-second latency, check the thread's checkpoint count and the troubleshooting section in [compaction-and-checkpoints.md](./compaction-and-checkpoints.md).

---

### Get Thread Context Stats

```http
GET /threads/{thread_id}/context
Authorization: Bearer <token>
```

Get context window usage statistics for a thread.

**Response:**
```json
{
  "thread_id": "abc123",
  "total_tokens": 45000,
  "input_tokens": 30000,
  "output_tokens": 15000,
  "context_limit": 200000,
  "usage_percentage": 22.5,
  "compaction_count": 1,
  "last_compaction": "2025-01-15T10:30:00Z",
  "context_management": "auto_compact"
}
```

---

### Delete Thread

```http
DELETE /threads/{thread_id}
Authorization: Bearer <token>
```

Fully deletes a thread and all thread-bound resources that could wake, route,
or recreate it later. The cascade removes checkpoints, metadata, config,
notepad content, RAG chunks, TODOs and schedule rows, triggers and execution
logs, chat-app bindings, pending bind codes, owner rows, activity entries,
notifications, and FCM device thread filters.

Account-level resources are preserved: users, tokens, platform identities,
registered Telegram bots, profile memories, global skills, and custom tools are
not deleted.

**Response:**
```json
{
  "status": "ok",
  "thread_id": "abc123",
  "deleted": {
    "checkpoints_deleted": 4,
    "todos_deleted": 1,
    "scheduled_todos_deleted": 1,
    "triggers_deleted": 0,
    "chat_bindings_deleted": 1,
    "thread_owners_deleted": 1,
    "checkpoint_rows_remaining": 0
  },
  "warnings": []
}
```

`deleted` is a count map by storage surface. The request fails if an active
wake/routing surface (TODOs, schedule rows, triggers, chat bindings, owner
rows, callable config) cannot be cleaned, or if checkpoint deletion cannot be
verified. A non-empty `warnings` list means a non-critical display/search
surface failed after the critical cleanup completed.

---

### Compact Thread

```http
POST /threads/{thread_id}/compact?user_id=default
Authorization: Bearer <token>
```

Manually trigger context compaction for a thread.

**Response:**
```json
{
  "success": true,
  "messages_removed": 42,
  "summary_pending": true,
  "summary": "Summary of prior context..."
}
```

Manual compaction persists a visible `compaction_notice` immediately. When `summary_pending` is `true`, the same summary will be attached to the user's next message in that thread.

---

### List Tools

```http
GET /tools
Authorization: Bearer <token>
```

**Response:** current thread-available tool list.

### List Optional Tools

```http
GET /tools/optional
Authorization: Bearer <token>
```

Returns optional tools that can be enabled per thread. The list is role-filtered:
developer-only diagnostics such as `hello_test` are visible to admins only.

### Default Tool Policy

```http
GET /tools/defaults
PUT /tools/defaults
DELETE /tools/defaults
Authorization: Bearer <token>
```

Manage the default tool set applied to newly created threads.

### Thread Callable Tools

```http
GET /threads/{thread_id}/callable-tools
Authorization: Bearer <token>
```

Returns the callable thread tools actually available from that caller thread after ownership filtering, callable-team scoping, self-exclusion, disabled-tool filtering, and tool-name conflict suppression. The response shape is:

```json
{
  "thread_id": "thread-123",
  "callable_thread_count": 2,
  "callable_threads": [
    {
      "thread_id": "agent-456",
      "name": "Researcher",
      "description": "Research support",
      "team_id": "team-a",
      "team_name": "Research"
    }
  ]
}
```

---

## SSE Event Types

| Type | Description | Fields |
|------|-------------|--------|
| `thinking` | Agent reasoning / extended-thinking text | `content` |
| `tool_call_delta` | Status-only hint that the model is streaming tool-call argument chunks before the tool starts | none |
| `tool_call` | Tool being invoked | `name`, `args` |
| `tool_result` | Tool execution result | `name`, `result` |
| `workspace_artifact` | Downloadable file generated by a tool (for example `file_write(..., attach=True)`) | `tool_call_id`, `tool_name`, `path`, `name`, `mime_type`, `size_bytes` |
| `tool_reload` | Tool registry was reloaded mid-turn; resume metadata for next iteration | `tools`, `ttl`, `ttl_seconds`, `source`, `skill_name`, `reason` |
| `dispatched` | Leading `@thread` mention routed the turn to another thread | `target_thread_id`, `title`, `matched_ref`, `dispatched_to` |
| `response` | Visible assistant text chunk. May appear before a `tool_call` as preamble/commentary, or after tools as the final answer. | `content` |
| `context_attached` | Previous context summary attached to this message | `summary` |
| `compacting` | Context summary generation has started | `message` |
| `compacted` | Context was compacted; async streams may resume afterward | `messages_removed`, `auto_resumed`, `summary` |
| `queued` | Thread is busy with another turn; client should wait | `content` |
| `iteration_limit` | Agent hit a turn safety stop: either the max tool-call budget or repeated same tool/args/result loop detection | `content`, `reason`, `max_iterations`, `tool_call_count`, optional `repeated_tool_name`, `repeated_count` |
| `error` | Error message | `content` |
| `done` | Stream complete | `context_stats`, `model` (when available) |

All events include `thread_id` for correlation.

### Command Service

Most slash commands are handled by the central command service and return
markdown. Desktop, mobile, bots, and the agent `slash_command` tool use the
same registry. In-process callers execute through the direct backend adapter;
out-of-process surfaces call `POST /commands/execute`.

```http
GET /commands?actor=user&surface=desktop
```

`actor` may be `user`, `agent`, or `system`. `surface` may be `desktop`,
`mobile`, `cli`, `discord`, `telegram`, `slack`, `matrix`, `whatsapp`, `messenger`, `instagram`, `webex`,
`mattermost`, `zulip`, `rocketchat`, `teams`, `googlechat`, `line`, `signal`,
`api`, or `agent` (`twitch` is still accepted for compatibility, but the Twitch
bot is deprecated). The legacy
`source=user|agent|cli` query parameter still works; `actor` and `surface`
are preferred for new callers. Agent actor hides commands whose metadata marks
them unavailable to agents. Non-admin users do not see admin-only commands.

**Response:**

```json
[
  {
    "id": "tools.core",
    "path": ["tools", "core"],
    "name": "tools core",
    "description": "Inspect or change thread tools",
    "usage": "/tools core",
    "category": "Tools",
    "subcommands": [],
    "aliases": ["/tools_core"],
    "scope": "global",
    "surfaces": ["desktop", "mobile", "cli", "discord", "telegram", "slack", "matrix", "whatsapp", "messenger", "instagram", "webex", "mattermost", "zulip", "rocketchat", "teams", "googlechat", "line", "signal", "api", "agent"],
    "agent_allowed": true,
    "requires_thread": false,
    "requires_admin": false,
    "mutates_state": false,
    "danger_level": "safe",
    "execution_kind": "command",
    "note": null
  }
]
```

```http
POST /commands/execute
```

**Request:**

```json
{
  "command": "/tools core",
  "thread_id": "abc123",
  "source": "user",
  "actor": "user",
  "surface": "desktop"
}
```

**Response:**

```json
{
  "success": true,
  "markdown": "### Core Tools\n\n...",
  "command": "tools core",
  "level": "success",
  "data": null
}
```

Commands that require an active thread return a markdown error if `thread_id`
is omitted. `/compact` appears in discovery with `execution_kind:
"chat_stream"`, but `POST /commands/execute` returns a markdown error telling
the caller to route it through chat streaming instead.

### Chat Slash Commands

The `/chat` endpoint keeps streaming slash commands that are not simple
request/response commands. Send the command as the message:

| Command | Description |
|---------|-------------|
| `/compact` | Manually trigger context compaction. Emits `compacting`, `compacted`, then a `response` confirmation. |

Example:
```json
{"message": "/compact", "thread_id": "abc123"}
```

Response:
```
data: {"type": "compacting", "message": "Compacting context...", "thread_id": "abc123"}
data: {"type": "compacted", "messages_removed": 42, "auto_resumed": false, "summary": "...", "thread_id": "abc123"}
data: {"type": "response", "content": "✓ Conversation compacted. 42 messages summarized."}
data: {"type": "done", "thread_id": "abc123", "context_stats": {...}, "model": "..."}
```

After manual `/compact`, the summary is attached to the user's **next** message. Automatic async compaction differs: after `compacted`, Nymeria streams the resumed assistant output immediately below the compaction notice.

---

---

### Autonomous Task Stream (SSE)

```http
GET /autonomous/stream?user_id=<user_id>&client_id=<client_id>
Authorization: Bearer <token>
```

**Auth:** Desktop and mobile use `fetch()` streaming with `Authorization: Bearer <token>` and `Accept: text/event-stream`. The legacy `api_key` query parameter is still accepted for older EventSource clients, but new clients should not put tokens in the URL.

Connects to a Server-Sent Events stream for receiving real-time updates during autonomous task execution: scheduled TODOs, trigger actions, callable-thread runs, spawned-thread runs, and `/chat` calls with `is_self_invoke=true`.

**Client behavior:** Treat this as a long-lived fetch stream, not a finite request. Heartbeats are SSE comments (`: heartbeat`) and do not carry JSON. Clients should reconnect when the response ends, errors, or stops receiving heartbeat/data bytes. The desktop client also refreshes current thread history/context and the thread list after reconnect so missed autonomous chunks are reconciled from persisted state.

Every data frame is a JSON object with canonical `type`, `thread_id`, `task_id`,
and `timestamp` fields plus the event-specific payload. Internal fields such as
`_origin_client_id` are stripped before the event is sent to clients.

**Query Parameters:**
| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `user_id` | No | `"default"` | Legacy/user hint. For normal Bearer auth, the authenticated account is authoritative; the query value does not grant access to another user's events. |
| `client_id` | No | - | Frontend client ID for filtering same-client sync events. Desktop and mobile send a per-session UUID. |
| `api_key` | No | - | Legacy fallback token for clients that cannot set headers |

**Auth and filtering:**
- Normal users stream only their own events, regardless of the `user_id` query.
- Admin callers can set `X-Nymeria-Act-As: <user_id>` to stream one target
  user's events.
- Admin callers can set `X-Nymeria-Act-As: *` for the firehose. This is used by
  Discord and Telegram thin clients with the admin service token; they route
  events to platform chats by inspecting `thread_id` prefixes. Non-admin callers
  that send any `X-Nymeria-Act-As` value receive `403`.
- `client_id` deduplicates cross-client sync events. Use the same value as
  `X-Nymeria-Client-Id` on mutating requests; events with a matching hidden
  origin are filtered from this stream.

**Ordering rules:**
1. `task_started` is the first autonomous event for a run, but it is deferred
   until the first non-`queued` agent chunk after the thread lock is acquired.
2. Supported agent chunks are forwarded in the order produced by the agent.
3. `task_completed` closes the run and is emitted on both success and failure.
   Consumers should render live `response` chunks as the primary output and use
   `task_completed.content` only as a fallback when no response chunks arrived.

**Event Types:**

| Event | Description | Fields |
|-------|-------------|--------|
| `task_started` | Autonomous execution begins | `thread_id`, `task_id`, `prompt`, optional `todo_id`, `trigger_id`, `trigger_name`, `callable_name`, `source`, `handoff_id`, `caller_thread_id`, `caller_thread_name` |
| `thinking` | Agent reasoning | `content` |
| `tool_call_delta` | Status-only hint that the model is streaming tool-call argument chunks before the tool starts | none |
| `tool_call` | Tool invocation | `id`, `name`, `args` |
| `tool_result` | Tool execution result | `id`, `name`, `result` |
| `workspace_artifact` | Downloadable workspace file generated during the task | `tool_call_id`, `tool_name`, `path`, `name`, `mime_type`, `size_bytes` |
| `tool_reload` | Tool registry was reloaded mid-turn; resume metadata for next iteration | `tools`, `ttl`, `ttl_seconds`, `source`, `skill_name`, `reason` |
| `response` | Response text chunks | `content` |
| `context_attached` | Previous context summary attached to this autonomous prompt | `summary` |
| `compacting` | Context summary generation has started | `message` |
| `compacted` | Context was compacted | `messages_removed`, `auto_resumed`, `summary` |
| `iteration_limit` | Agent hit a turn safety stop | `content`, `reason`, `max_iterations`, `tool_call_count`, optional repeated-tool fields |
| `notification` | Explicit `notify` tool event or new in-app notification | `message`, `summary`, `in_app_only` |
| `task_completed` | Execution finished | `notify`, `content`, `summary`, `todo_id`, optional `handoff_id`, `caller_thread_id`, `caller_thread_name` |

The same stream also carries cross-client sync events used by open frontends:

| Event | Description | Fields |
|-------|-------------|--------|
| `message_added` | Another client posted an interactive user message | `role`, `content` |
| `thread_created` | A thread/callable thread was created | `title`, `title_source`, `platform` |
| `thread_updated` | Thread metadata changed | `title`, `title_source`, `pinned`, `platform` |
| `thread_deleted` | A thread was deleted | none |

**Example Stream:**
```
: heartbeat
data: {"type":"task_started","thread_id":"abc123","task_id":"todo-xyz","prompt":"Check inbox","todo_id":"xyz"}
data: {"type":"thinking","content":"I'll check the inbox now..."}
data: {"type":"tool_call","id":"tool1","name":"bash_execute","args":{"command":"ls ~/inbox"}}
data: {"type":"tool_result","id":"tool1","name":"bash_execute","result":"email1.txt\nemail2.txt"}
data: {"type":"tool_reload","tools":["inbox_read"],"ttl":"2h","ttl_seconds":7200,"source":"skill_kit","skill_name":"inbox-kit","reason":"required by skill"}
data: {"type":"workspace_artifact","tool_call_id":"tool2","tool_name":"file_write","path":"/workspace/report.csv","name":"report.csv","mime_type":"text/csv","size_bytes":1024}
data: {"type":"response","content":"Found 2 new emails in inbox."}
data: {"type":"task_completed","notify":false,"content":"Found 2 new emails.","todo_id":"xyz"}
: heartbeat
```

**Heartbeat:** Sent every ~1 second when no events to keep connection alive.

**Operational diagnostics:** A healthy live path produces log lines for each hop:
- Worker/API publish: `[REDIS EVENT BUS] publish ...`
- API Redis receive: `[REDIS EVENT BUS] message_received ... local_subscribers=N`
- API local enqueue: `[REDIS EVENT BUS] queue_enqueue ...`
- API stream receive/yield: `[AUTONOMOUS SSE] queue_receive ...` then `[AUTONOMOUS SSE] yield ...`
- Desktop receive: `[Autonomous] First data event frame received ...` and sampled `[Autonomous] Event handled type=...`

---

## Multi-User Support

The `user_id` field enables per-user memory isolation:

- Each `user_id` has its own profile and memories
- Memories saved by user A are not visible to user B
- Default is `"default"` for single-user deployments
- Useful for multi-tenant applications sharing one API

**Example:**
```json
{
  "message": "My name is Alex",
  "user_id": "user_123"
}
```

This will save the name to user_123's profile, isolated from other users.

---

## Example: Python Client

### Synchronous Request

```python
import httpx

response = httpx.post(
    "http://localhost:8000/chat/sync",
    json={
        "message": "Hello",
        "user_id": "my_user"
    },
    headers={"Authorization": "Bearer dev-key"}
)
print(response.json()["response"])
```

### Streaming Request

```python
import httpx

with httpx.stream(
    "POST",
    "http://localhost:8000/chat",
    json={"message": "Search for Python tutorials"},
    headers={"Authorization": "Bearer dev-key"}
) as r:
    for line in r.iter_lines():
        if line.startswith("data: "):
            import json
            event = json.loads(line[6:])

            if event["type"] == "response":
                print(event["content"], end="", flush=True)
            elif event["type"] == "tool_call":
                print(f"\n[Calling {event['name']}...]")
            elif event["type"] == "done":
                print("\n[Done]")
```

### Async Client

```python
import httpx
import asyncio

async def chat():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/chat/sync",
            json={"message": "Hello"},
            headers={"Authorization": "Bearer dev-key"}
        )
        return response.json()

result = asyncio.run(chat())
print(result["response"])
```

---

### Get Settings

```http
GET /settings
Authorization: Bearer <token>
```

**Response:** includes LLM settings such as `llm_provider`, `llm_model`, `llm_base_url`, `openai_api_mode`, and LLM stream retry settings; context settings such as `context_management`, `compact_threshold`, and `compact_keep_messages`; tool runtime settings such as `tool_output_max_chars`; plus voice runtime settings such as `tts_provider`, `tts_base_url`, `tts_model`, `tts_voice`, `tts_output_format`, `tts_speed`, `stt_provider`, `stt_base_url`, `stt_model`, `stt_language`, and `voice_default_thread_id`.

Settings are server-wide. The authenticated user controls access to the endpoint, but the returned LLM provider/model/base URL are not scoped to that user. Provider and capability API keys are not included in this response.

The admin environment endpoints (`GET /settings/env` and `GET /settings/env/{key}`) omit the retired `NYMERIA_API_KEY` shared-token setting. The field can still exist in old `.env` files for validation compatibility, but account tokens are authoritative and the legacy value is not part of the configuration API.

### LLM Runtime Diagnostics

```http
GET /settings/llm/runtime
Authorization: Bearer <token>
```

Returns the currently active runtime provider/model, effective max tokens, source env files, and OpenRouter key diagnostics when applicable.

---

### Test LLM Provider Settings

```http
POST /settings/llm/test
Content-Type: application/json
Authorization: Bearer <admin-token>
```

Admin-only. Tests a provider/model/key/base-URL combination without writing it
to config. The response never echoes the submitted API key.

```json
{
  "llm_provider": "openai",
  "llm_model": "gpt-5.5",
  "api_key": "cpx-...",
  "llm_base_url": "http://localhost:8317/v1",
  "openai_api_mode": "responses"
}
```

For Anthropic CLIProxy, pass the root proxy URL without `/v1`; for
OpenAI/Codex CLIProxy, pass the OpenAI-compatible `/v1` URL. A failed provider
probe still returns `200` with `"ok": false` and sanitized error text so setup
UIs can display the provider failure without treating the Nymeria API request
itself as broken.

---

### LLM Provider Test Suite

```http
POST /settings/llm/test-suite
Content-Type: application/json
Authorization: Bearer <admin-token>
```

Admin-only. Runs the production-readiness provider suite without writing
settings. Unlike `/settings/llm/test`, `api_key` is optional: the suite resolves
auth from the submitted key, then the encrypted credential vault, then
settings/env. It checks provider metadata, base URL, credential availability,
the provider `/models` endpoint, a tiny non-streaming chat completion, and a
forced tool-call request when enabled.

By default, live generation is skipped unless the selected model is known to be
free, the endpoint is local/no-key, or `allow_billable` is set to `true`.

```json
{
  "llm_provider": "openrouter",
  "llm_model": "meta-llama/llama-3.1-8b-instruct:free",
  "api_key": "sk-or-...",
  "openai_api_mode": "chat_completions",
  "run_model_list": true,
  "run_chat_completion": true,
  "run_tool_call": true,
  "allow_billable": false,
  "prefer_free_model": true,
  "timeout_seconds": 15
}
```

The response is a sanitized report with top-level `ok`, selected `model`,
effective base URL/API mode, credential source, model count, and ordered step
results. Failed provider calls still return HTTP `200` with `ok: false`; API
authorization failures still return normal Nymeria API errors.

---

### LLM Provider Catalog

```http
GET /settings/llm/providers
Authorization: Bearer <token>
```

Returns Nymeria's provider registry: provider IDs, labels, default base URLs,
API key env vars, aliases, Chat Completions support, and Responses support.
Desktop/mobile use this metadata for provider setup and diagnostics; the
authoritative implementation lives in `nymeria/config/llm_providers.py`.

---

### Available Provider Models

```http
GET /models/available?provider=groq&base_url=https://api.groq.com/openai/v1
Authorization: Bearer <token>
```

Fetches models from the selected provider's live `/models` endpoint. `provider`
defaults to the server's `LLM_PROVIDER`; `base_url` is optional and lets the UI
test unsaved OpenAI-compatible endpoint overrides. The endpoint resolves auth
from the credential vault first, then settings/env vars. Returned model objects
include `id`, `name`, `owned_by`, `created`, and any normalized metadata the
provider exposes, such as `context_length`, `max_completion_tokens`,
`supported_parameters`, and `input_modalities`.

Chat Completions responses standardize usage token fields, but model context
window is not standardized. When `/models/available` sees context metadata,
Nymeria caches it so `/threads/{thread_id}/context` can report frontend context
percentage more accurately.

---

### Update Settings

```http
PATCH /settings
Content-Type: application/json
Authorization: Bearer <admin-token>
```

**Request Body:** (all fields optional)
```json
{
  "llm_provider": "openrouter",
  "llm_model": "anthropic/claude-sonnet-4",
  "llm_fallback_models": ["anthropic:claude-haiku-4-5-20251001"],
  "llm_temperature": 0.7,
  "llm_max_tokens": 4096,
  "llm_top_p": 0.95,
  "llm_top_k": 40,
  "llm_frequency_penalty": 0.5,
  "llm_presence_penalty": 0.3,
  "llm_reasoning_effort": "medium",
  "llm_use_model_defaults": false,
  "openai_api_mode": "responses",
  "openai_api_key": "sk-...",
  "anthropic_api_key": "sk-ant-or-cpx-...",
  "anthropic_direct_api_key": "sk-ant-...",
  "openrouter_api_key": "sk-or-...",
  "embedding_api_key": "sk-...",
  "gemini_api_key": "AIza...",
  "perplexity_api_key": "pplx-...",
  "llm_stream_max_retries": 2,
  "llm_stream_retry_initial_delay": 1.0,
  "llm_stream_retry_max_delay": 8.0,
  "tool_output_max_chars": 100000
}
```

| Field | Type | Range | Description |
|-------|------|-------|-------------|
| `llm_provider` | string | - | Provider ID. `anthropic` uses Anthropic Messages; OpenAI-compatible IDs are listed by `GET /settings/llm/providers`. |
| `llm_model` | string | - | Model identifier |
| `llm_fallback_models` | string[] / comma string on PATCH | - | Ordered backend model fallback chain. PATCH accepts a comma-separated string; entries may be `model-id` or `provider:model-id`. |
| `llm_temperature` | float | 0.0-2.0 | Sampling temperature |
| `llm_max_tokens` | int | 1-1000000 | Max output tokens |
| `llm_top_p` | float | 0.0-1.0 | Nucleus sampling threshold |
| `llm_top_k` | int | 1-100 | Top-k sampling |
| `llm_frequency_penalty` | float | -2.0-2.0 | Reduce repetition |
| `llm_presence_penalty` | float | -2.0-2.0 | Encourage new topics |
| `llm_reasoning_effort` | string | low/medium/high | For reasoning models |
| `llm_use_model_defaults` | bool | true/false | Use model-specific defaults for temperature/top_p/frequency_penalty |
| `openai_api_mode` | string | `responses`/`chat_completions` | Default OpenAI-compatible API mode. `responses` is honored only for registry providers that advertise Responses support; Chat Completions is the compatibility baseline. |
| `openai_api_key` | string | - | Write-only OpenAI or OpenAI-compatible global API key |
| `anthropic_api_key` | string | - | Write-only Anthropic global key. For Anthropic CLIProxy this is the local `cpx-*` gatekeeper key. |
| `anthropic_direct_api_key` | string | - | Write-only direct Anthropic key used when the effective Anthropic base URL is empty |
| `openrouter_api_key` | string | - | Write-only OpenRouter global API key |
| `embedding_api_key` | string | - | Write-only OpenAI-compatible embeddings key. Do not set this to a CLIProxy `cpx-*` gatekeeper key. |
| `gemini_api_key` | string | - | Write-only Gemini key for Gemini-backed capabilities |
| `perplexity_api_key` | string | - | Write-only Perplexity key for web search |
| `llm_stream_max_retries` | int | 0-10 | Retries for transient LLM call/stream failures before any model output is emitted |
| `llm_stream_retry_initial_delay` | float | 0-60 | Initial LLM retry backoff delay in seconds |
| `llm_stream_retry_max_delay` | float | 0-300 | Maximum LLM retry backoff delay in seconds |
| `tool_output_max_chars` | int | 1000-2000000 | Max stored characters per tool result; larger outputs keep head and tail with a marker |

**Response:**
```json
{
  "message": "Settings updated and applied",
  "updated": ["llm_model", "llm_temperature"],
  "restart_required": false
}
```

**Note:** This endpoint is admin-only. Changes are written to the highest-precedence existing runtime config file (`.env.docker`, `config.env`, then `.env`), hot-reloaded immediately, and apply to every user on the server unless a thread has its own LLM override. Credential values are accepted in the request but are not returned by `GET /settings` or the update response; the admin env listing masks secret values.

Native optional tools prefer saved credentials from the user/system credential vault. The same settings endpoint can still manage deployment-wide fallback env fields for integrations, including `DHL_API_KEY`, `ONFLEET_API_KEY`, `PHANTOMBUSTER_API_KEY`, `WEBFLOW_ACCESS_TOKEN`, `LEMLIST_API_KEY`, `SENDY_API_KEY`, `EMELIA_API_KEY`, `AFFINITY_API_KEY`, `KEAP_ACCESS_TOKEN`, `MAGENTO_ACCESS_TOKEN`, `UNLEASHED_API_KEY`, `DRIFT_ACCESS_TOKEN`, `OKTA_ACCESS_TOKEN`, `MAUTIC_BASE_URL`, `RUNDECK_BASE_URL`, `RUNDECK_TOKEN`, `KOBOTOOLBOX_API_TOKEN`, `KOBOTOOLBOX_BASE_URL`, `QUICKBOOKS_ACCESS_TOKEN`, `QUICKBOOKS_REALM_ID`, `XERO_ACCESS_TOKEN`, `XERO_TENANT_ID`, `MICROSOFT_GRAPH_ACCESS_TOKEN`, and `MICROSOFT_GRAPH_BASE_URL`; see `docs/tools.md` for the exact provider and field names.

---

### List OpenRouter Models

```http
GET /models
Authorization: Bearer <token>
```

Returns cached OpenRouter model metadata. The backend fetches model data from the OpenRouter API and caches it for 1 hour. Returns an empty list if the cache hasn't been populated yet (non-critical enrichment data). Attachment validation uses this live metadata when available, then falls back to a static capability registry for known multimodal families.

**Response:**
```json
[
  {
    "id": "anthropic/claude-sonnet-4",
    "name": "Claude Sonnet 4",
    "context_length": 200000,
    "max_completion_tokens": 16384,
    "pricing_prompt": 0.000003,
    "pricing_completion": 0.000015,
    "supported_parameters": ["temperature", "top_p", "tools", "reasoning", "max_tokens"],
    "input_modalities": ["text", "image", "file"],
    "tokenizer": "Claude",
    "default_temperature": 1.0,
    "default_top_p": null,
    "default_frequency_penalty": null
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | OpenRouter model identifier (e.g. `anthropic/claude-sonnet-4`) |
| `name` | string | Human-readable model name |
| `context_length` | int | Maximum context window in tokens |
| `max_completion_tokens` | int \| null | Maximum output tokens (null if unknown) |
| `pricing_prompt` | float \| null | Cost per input token in USD |
| `pricing_completion` | float \| null | Cost per output token in USD |
| `supported_parameters` | string[] | API parameters the model accepts (used for smart parameter gating) |
| `input_modalities` | string[] | Supported input types: `text`, `image`, `file` |
| `tokenizer` | string \| null | Tokenizer family: `Claude`, `GPT`, `Llama3`, etc. |
| `default_temperature` | float \| null | Model's default temperature (shown when "Use model defaults" is enabled) |
| `default_top_p` | float \| null | Model's default top_p |
| `default_frequency_penalty` | float \| null | Model's default frequency penalty |

---

### List Threads

```http
GET /threads?user_id=default
Authorization: Bearer <token>
```

Returns all threads with server-authoritative metadata (titles, pins, platform).
When a thread's saved config has `callable=true`, the list response reports
`callable=true` and uses `callable_name` as the title. `platform` remains the
thread's visible origin surface for native or explicitly bound chat-app
threads such as Telegram; otherwise callable threads report
`platform="callable"` even if older thread metadata still says `desktop`. If
callable is disabled, the list response reports `callable=false` and does not
keep stale `platform="callable"` metadata in the payload.
The list also includes recoverable thread IDs referenced by thread-bound
resources such as TODOs, scheduled TODO rows, triggers, chat bindings, bind
codes, and safe orphan checkpoints. Those rows are marked with
`recovered=true` so clients can surface partially-deleted threads for cleanup
instead of hiding them. Recovered rows are filtered to thread IDs the effective
caller can actually open through the detail routes; stale metadata for a thread
owned by another user is not returned.

**Response:**
```json
{
  "threads": [
    {
      "id": "abc123",
      "title": "Research Python tutorials",
      "title_source": "auto",
      "pinned": false,
      "platform": "desktop",
      "callable": false,
      "created_at": "2026-02-27T10:00:00Z",
      "updated_at": "2026-02-27T10:30:00Z",
      "recovered": false,
      "recovery_sources": []
    }
  ]
}
```

| Field | Description |
|-------|-------------|
| `title` | Server-authoritative display title |
| `title_source` | `"auto"` (generated from first message), `"user"` (manual rename), `"callable"` (synced from callable_name) |
| `pinned` | Whether thread is pinned to top |
| `platform` | Origin surface: `"desktop"`, `"callable"`, `"discord"`, `"telegram"`, `"slack"`, `"matrix"`, `"whatsapp"`, `"messenger"`, `"instagram"`, `"webex"`, `"mattermost"`, `"zulip"`, `"rocketchat"`, `"teams"`, `"googlechat"`, `"line"`, `"signal"`, `"twitch"`, `"trigger"`, `"webhook"` |
| `callable` | Whether the saved per-thread config currently marks the thread callable |
| `recovered` | `true` when this row was included because a resource survived without the normal complete thread listing path and the effective caller can open it |
| `recovery_sources` | Storage surfaces that referenced the recovered thread, e.g. `"metadata"`, `"todo"`, `"scheduled_todo"`, `"trigger"`, `"chat_binding"`, `"bind_code"`, `"checkpoint"` |

---

### Update Thread Metadata

```http
PATCH /threads/{thread_id}/metadata?user_id=default
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** (all fields optional)
```json
{
  "title": "New title",
  "pinned": true
}
```

Updates the thread's server-side metadata. If the thread is callable, renaming also updates its `callable_name` in thread config and rebuilds the tool registry (so the LLM sees the new tool name). Name collisions with core tools or other callables are silently skipped.

**Response:**
```json
{
  "status": "ok",
  "thread_id": "abc123",
  "updated_fields": ["title", "title_source"]
}
```

---

### Branch Thread

```http
POST /threads/{thread_id}/branch
Content-Type: application/json
Authorization: Bearer <token>
```

Creates a new owned thread by copying the source thread's checkpoint history
and saved per-thread config (instructions, model/provider overrides, enabled
or disabled tools, skills, and related thread settings). If the source thread
is callable, the branch receives a deduplicated callable name so it does not
collide with the source tool registration.

**Request Body:** (all fields optional)
```json
{
  "title": "Alternate approach",
  "from_message_index": 6
}
```

`from_message_index` is 1-based and selects the latest checkpoint at or before
that raw checkpoint message count. Omit it to copy the current full checkpoint
history. The endpoint returns `409` if the source thread is actively
processing.

**Response:**
```json
{
  "status": "ok",
  "source_thread_id": "abc123",
  "thread_id": "branch-1a2b3c4d5e6f",
  "title": "Alternate approach",
  "requested_title": "Alternate approach",
  "from_message_index": 6,
  "source_checkpoint_id": "1f14c493-...",
  "config_cloned": true,
  "callable_name": null,
  "checkpoints": {
    "checkpoints_copied": 12,
    "writes_copied": 4,
    "checkpoint_writes_copied": 0,
    "checkpoint_blobs_copied": 0
  },
  "metadata": {}
}
```

---

### Stop Thread

```http
POST /threads/{thread_id}/stop
Authorization: Bearer <token>
```

Aborts a running stream on the thread. Cascades to any active callable child threads.

**Response:**
```json
{
  "status": "ok",
  "message": "Abort signal sent for thread abc123"
}
```

---

### Claim Thread Ownership

```http
POST /threads/{thread_id}/claim
Authorization: Bearer <token>
```

Eagerly registers the calling user as the owner of `thread_id` in the `thread_owners` table. The desktop frontend calls this from `threadsStore.createThread()` immediately after generating a UUID, so the backend has an ownership row before any chat-app routing (Telegram/Discord via `X-Nymeria-Act-As`) can hit `/chat` and TOFU-claim the thread for someone else.

Idempotent — safe to call multiple times.

**Response (200):**
```json
{
  "thread_id": "abc123",
  "owner": "default"
}
```

**Errors:**
- `400` — `thread_id` matches a shared-channel pattern (`discord_<g>_<c>`, `telegram_-<id>`, `twitch_<c>`, `slack_C...`, `matrix_!room...`, `whatsapp_group_<id>`, `webex_<room>`, `mattermost_<server>_<channel>`, `zulip_<realm>_<stream>`, `rocketchat_<server>_<room>`, `signal_group_<group>`). These are inherently multi-user and cannot be per-user-claimed.
- `404` — Non-admin caller and the thread is owned by someone else. Mirrors `_require_thread_access`'s leak surface so callers can't probe ownership under other users. Admin callers always get `200` with the actual owner instead.

See [`accounts.md` → Thread ownership](accounts.md#thread-ownership) for the full lifecycle.

---

## TODO Management API

Manage TODO items with optional scheduling for autonomous execution.

TODO routes resolve the user from the Bearer token. Admin callers can target a
specific user with `X-Nymeria-Act-As`; client-supplied `?user_id=` values are
ignored by these routes for account isolation.

Completed TODOs are retained in the active TODO JSON list until ticker cleanup
removes completed items older than `TODO_AUTO_ARCHIVE_DAYS` (default 7 days).
Cleanup removes them from `data/todos/{user_id}.json`; it does not write a
separate completed-TODO archive file.

### List Users with TODOs

```http
GET /todos/users
Authorization: Bearer <token>
```

Returns all user IDs whose TODO file currently contains at least one TODO item. Empty legacy or stale TODO files are ignored so the watchdog worker does not poll no-op lists or try to impersonate deleted platform-only users.

No query parameters.

**Response:**
```json
["default", "discord_699436710118817823", "telegram_5551234567"]
```

---

### List TODOs

```http
GET /todos?filter_status=all
Authorization: Bearer <token>
```

**Query Parameters:**
| Parameter | Default | Description |
|-----------|---------|-------------|
| `filter_status` | active only | Filter: `all`, `pending`, `in_progress`, `done` |
| `thread_id` | - | Filter TODOs to a specific thread |

**Response:**
```json
{
  "user_id": "default",
  "items": [
    {
      "id": "abc123",
      "task": "Check inbox",
      "status": "pending",
      "created_at": "2026-02-02T10:00:00Z",
      "updated_at": "2026-02-02T10:00:00Z",
      "scheduled_for": "2026-02-02T14:00:00Z",
      "thread_id": "thread-xyz",
      "recurrence": "daily",
      "created_by": "user"
    }
  ],
  "total": 1
}
```

---

### Create TODO

```http
POST /todos
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "task": "Check inbox",
  "scheduled_for": "30m",
  "recurrence": "daily",
  "thread_id": "optional-thread-id",
  "notes": "Optional notes"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task` | string | Yes | Task description |
| `scheduled_for` | string | No | Relative (`45s`, `17m`, `2h`, `1w`) or absolute/ISO datetime |
| `recurrence` | string | No | Valid recurrence pattern |
| `thread_id` | string | No | Thread for autonomous output, defaults to a user-scoped default thread |
| `notes` | string | No | Additional context |

**Response:** Created TODO object

---

### Update TODO

```http
PATCH /todos/{todo_id}
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** (all fields optional)
```json
{
  "task": "Updated task",
  "status": "in_progress",
  "scheduled_for": "2h",
  "thread_id": "thread-xyz",
  "clear_schedule": false,
  "recurrence": "weekly",
  "clear_recurrence": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `task` | string | Updated task text |
| `status` | string | `pending`, `in_progress`, `done` |
| `notes` | string | Set or update notes |
| `scheduled_for` | string | Set or update next scheduled execution |
| `recurrence` | string | Set or update recurrence pattern |
| `thread_id` | string | Thread for autonomous output |
| `clear_schedule` | bool | Remove schedule if `true` |
| `clear_recurrence` | bool | Remove recurrence if `true` |

**Response:** Updated TODO object

**Conflict:** Returns `409 Conflict` if the TODO is currently executing as a scheduled autonomous run. Retry after the run finishes.

---

### Complete TODO

```http
POST /todos/{todo_id}/complete
Authorization: Bearer <token>
```

Marks the TODO as done. Recurring TODOs are rescheduled by the backend rather than simply being unscheduled. Non-recurring completed TODOs remain listable with `filter_status=done` until the configured ticker cleanup window expires.

**Response:** Updated TODO object

**Conflict:** Returns `409 Conflict` if the TODO is currently executing as a scheduled autonomous run. Retry after the run finishes.

---

### Delete TODO

```http
DELETE /todos/{todo_id}
Authorization: Bearer <token>
```

**Response:**
```json
{
  "status": "ok",
  "deleted_id": "abc123"
}
```

**Conflict:** Returns `409 Conflict` if the TODO is currently executing as a scheduled autonomous run. Retry after the run finishes.

---

## Activity Log API

### Get Activity

```http
GET /activity?user_id=default&limit=50
Authorization: Bearer <token>
```

Returns recent activity entries (autonomous tasks, tool executions, etc.).

**Response:**
```json
{
  "entries": [
    {
      "id": "entry123",
      "timestamp": "2026-02-02T10:00:00Z",
      "type": "task_completed",
      "message": "Checked inbox, found 2 emails",
      "thread_id": "thread-xyz",
      "metadata": {"todo_id": "abc123"}
    }
  ],
  "total": 1
}
```

---

## Notifications API

### Get Notifications

```http
GET /notifications?user_id=default
Authorization: Bearer <token>
```

**Response:**
```json
{
  "notifications": [
    {
      "id": "notif123",
      "summary": "Found important email from boss",
      "thread_id": "thread-xyz",
      "task_id": "todo-abc",
      "created_at": "2026-02-02T10:00:00Z",
      "read": false
    }
  ],
  "unread_count": 1
}
```

### Mark Notification Read

```http
POST /notifications/{notification_id}/read?user_id=default
Authorization: Bearer <token>
```

### Mark All Read

```http
POST /notifications/read-all?user_id=default
Authorization: Bearer <token>
```

---

## Workspace API

### Download Workspace File

Download a file from the workspace directory. Used by bot clients and the desktop/mobile artifact viewers to deliver `file_write(..., attach=True)` outputs to users.

```http
GET /workspace/download?path=/workspace/report.csv
Authorization: Bearer <admin-token>
```

**Query Parameters:**
- `path` (required): Absolute file path within the workspace directory.

**Responses:**
- `200`: File content with appropriate `Content-Type` and `Content-Disposition` headers.
- `403`: Path is outside the workspace directory.
- `404`: File does not exist.

**Security:** Admin-only. Only files within `NYMERIA_WORKSPACE_DIR` (default `/workspace`) can be served. Paths are resolved and checked against the workspace root to prevent traversal. This same workspace boundary is what controls whether `file_write(..., attach=True)` produces a deliverable artifact at all.

---

## Custom Tools API

Manage custom HTTP and MCP tools programmatically.

### List Custom Tools

```http
GET /tools/custom
Authorization: Bearer <token>
```

**Response:**
```json
{
  "tools": [
    {
      "id": "get_weather",
      "name": "Get Weather",
      "description": "Get current weather for a city",
      "implementation_type": "http",
      "parameters": {
        "city": {
          "type": "string",
          "description": "City name",
          "required": true
        }
      },
      "http_config": {
        "method": "GET",
        "url": "https://api.weather.com/v1/current?city=${city}",
        "headers": {"Authorization": "Bearer ${env:WEATHER_API_KEY}"}
      },
      "enabled": true,
      "tags": ["weather", "api"],
      "created_at": "2026-02-02T10:00:00Z",
      "updated_at": "2026-02-02T10:00:00Z"
    }
  ],
  "total": 1
}
```

---

### Create Custom Tool

```http
POST /tools/custom
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body (HTTP Tool):**
```json
{
  "id": "get_weather",
  "name": "Get Weather",
  "description": "Get current weather for a city",
  "implementation_type": "http",
  "parameters": {
    "city": {
      "type": "string",
      "description": "City name",
      "required": true
    }
  },
  "http_config": {
    "method": "GET",
    "url": "https://api.weather.com/v1/current?city=${city}",
    "headers": {
      "Authorization": "Bearer ${env:WEATHER_API_KEY}"
    },
    "timeout_seconds": 30,
    "response_path": "$.data"
  },
  "enabled": true,
  "tags": ["weather"]
}
```

**Request Body (MCP Tool):**
```json
{
  "id": "read_file_mcp",
  "name": "Read File (MCP)",
  "description": "Read a file using MCP filesystem server",
  "implementation_type": "mcp",
  "parameters": {
    "path": {
      "type": "string",
      "description": "File path",
      "required": true
    }
  },
  "mcp_config": {
    "server_command": "npx",
    "server_args": ["-y", "@anthropic/mcp-server-filesystem", "/path"],
    "tool_name": "read_file",
    "idle_timeout_seconds": 300
  },
  "enabled": true
}
```

**Response:** The created tool object

---

### Get Custom Tool

```http
GET /tools/custom/{tool_id}
Authorization: Bearer <token>
```

**Response:** Single tool object

---

### Update Custom Tool

```http
PUT /tools/custom/{tool_id}
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** Same as create (all fields optional except those being updated)

**Response:** Updated tool object

---

### Delete Custom Tool

```http
DELETE /tools/custom/{tool_id}
Authorization: Bearer <token>
```

**Response:**
```json
{
  "message": "Tool 'get_weather' deleted"
}
```

---

### Test Custom Tool

```http
POST /tools/custom/{tool_id}/test
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "params": {
    "city": "London"
  }
}
```

**Response:**
```json
{
  "success": true,
  "result": "{\"temperature\": 15, \"conditions\": \"cloudy\"}",
  "execution_time_ms": 234
}
```

---

### Export/Import Custom Tools

**Admin-only.** Both endpoints require an admin bearer token because exports include full HTTP/MCP configs (URLs, headers, args).
`GET /tools/custom/export` and `POST /tools/custom/import` are literal
management routes and are matched before `{tool_id}` routes.

```http
GET /tools/custom/export
Authorization: Bearer <admin-token>
```

Returns all tools as a JSON array for backup/transfer.

```http
POST /tools/custom/import
Content-Type: application/json
Authorization: Bearer <admin-token>
```

Import tools from a JSON array.

---

## Callable Threads API

Callable threads replace the old sub-agent system. Any thread marked `callable=True` becomes a directly invocable tool — but only within threads owned by the **same user** that owns the callable. The tool registry is global, but `_build_graph_with_prompt` filters callables by ownership when building each user's graph, and the runtime gate in `agents/tool_factory.py` rejects cross-user invocations even on cache stale paths. Admins can route through another user's callables via `X-Nymeria-Act-As`.

Callable teams optionally narrow that owner-wide list. If the caller thread has `callable_team_id`, it only receives callable tools whose thread configs have the same `callable_team_id`. Threads without a team keep the legacy owner-wide callable list.

### List Callable Threads

```http
GET /agents/threads
Authorization: Bearer <token>
```

Returns callable thread configs. The response shape is `{"threads": [...], "total": N}`.

### Create Callable Thread

```http
POST /agents/threads
Authorization: Bearer <token>
Content-Type: application/json
```

Creates a callable thread directly from the API.

---

### Callable Teams

```http
GET /thread-teams
Authorization: Bearer <token>
```

Returns `{"teams": [{"id": "...", "name": "...", "thread_ids": [...]}], "total": N}` for the authenticated user's thread teams.

```http
POST /thread-teams
Authorization: Bearer <token>
Content-Type: application/json

{"name": "Ops", "thread_ids": ["thread-a", "thread-b"]}
```

Creates a team and moves the listed owned threads into it.

```http
PATCH /thread-teams/{team_id}
Authorization: Bearer <token>
Content-Type: application/json

{"name": "Ops Team", "thread_ids": ["thread-b", "thread-c"]}
```

Renames a team and/or replaces its membership. Moving a thread into a team removes it from any previous callable team.

```http
DELETE /thread-teams/{team_id}
Authorization: Bearer <token>
```

Deletes a team by clearing membership from its threads. Team changes invalidate cached graphs for the user's threads so subsequent turns rebuild callable-tool visibility.

---

### Thread Config (Per-Thread Settings)

Handlers live in `Nymeria/nymeria/api/routers/thread_config.py`; request
schemas live in `Nymeria/nymeria/api/schemas/thread_config.py`.

```http
GET /threads/{thread_id}/config
Authorization: Bearer <token>
```

Returns per-thread configuration including callable settings, custom instructions, LLM overrides, and tool enablement.

Use `GET /threads/{thread_id}/overview` when a caller needs resolved status,
effective LLM/tool/skill counts, TODO/trigger/chat-app summaries, or a compact
dashboard/header read model. Use `/config` when editing or exporting the
thread's saved settings.

```http
PATCH /threads/{thread_id}/config
Content-Type: application/json
Authorization: Bearer <token>
```

Updates thread config. Key fields for callable threads:

| Field | Type | Description |
|-------|------|-------------|
| `callable` | bool | Whether this thread is callable as a tool |
| `callable_name` | string | Tool name visible to the LLM (must be unique) |
| `callable_description` | string | Tool description shown to the LLM |
| `callable_team_id` | string | Optional callable visibility team id |
| `callable_team_name` | string | Optional callable visibility team display name |
| `custom_instructions` | string | System prompt for this thread |
| `disabled_tools` | array | Tool names to exclude |
| `enabled_tools` | array | Optional tool names to include |
| `llm_provider` | string | Override LLM provider |
| `llm_model` | string | Override model |
| `llm_config.base_url` | string | Per-thread provider base URL. For CLIProxy sidecars, this is the URL reachable from the Nymeria backend container, e.g. `http://cli-proxy-api-latest:8317/v1`. |
| `llm_config.api_key` | string | Per-thread provider API key. For CLIProxy sidecars, this is the local sidecar gatekeeper key, not an upstream OpenAI key. |
| `telegram_autonomous_delivery` | `"full" \| "notify_only" \| "off"` | Telegram delivery for autonomous outputs. Default `full`. |
| `in_app_notification_level` | `"notify_only" \| "all_autonomous" \| "off"` | Notification-center behavior. Default `notify_only`. |
| `llm_temperature` | float | Override temperature |
| `llm_config.openai_api_mode` | string | OpenAI-only API mode: `chat_completions` or `responses`. Use `responses` for CLIProxy Codex OAuth threads that need native Responses reasoning/tool blocks replayed from the checkpoint. |

**Callable thread naming:** The thread's sidebar title always equals `callable_name`. Renaming the thread via `PATCH /threads/{id}/metadata` automatically updates `callable_name` and rebuilds the tool registry.

```http
DELETE /threads/{thread_id}/config
Authorization: Bearer <token>
```

Resets thread config to defaults.

---

### Thread Config Sharing

```http
GET /threads/{thread_id}/export
Authorization: Bearer <token>
```

Exports a portable thread-share JSON document (`kind: "nymeria.thread.share"`, `version: 1`) for sharing a thread's configuration. The export includes the thread title and portable config such as custom instructions, system prompt override, callable settings, tool/skill enablement, LLM overrides, and prompt/debug/delivery flags.

The export intentionally omits conversation messages, notepad content, attachments, checkpoints, TODOs, triggers, chat-app bindings, temporary tool TTL state, and secrets such as `llm_config.api_key`.

```http
POST /threads/import
Content-Type: application/json
Authorization: Bearer <token>
```

Creates a new empty thread owned by the importing user from a `nymeria.thread.share` document. Imports do not overwrite an existing thread.

Import sanitization:

| Case | Behavior |
|------|----------|
| Missing tools, MCP tools, custom tools, or skills | Dropped from the imported config and returned in `warnings` |
| Admin-only enabled tools for non-admin importers | Dropped and returned in `warnings` |
| Callable name conflicts or invalid callable names | Replaced with a valid unique name and returned in `warnings` |
| `llm_config.api_key` in the file | Ignored and returned in `warnings` |

Response shape:

```json
{
  "status": "ok",
  "thread_id": "imported-abc123...",
  "title": "Imported Thread",
  "config": {},
  "warnings": []
}
```

---

## RAG Management API

Manage RAG (Retrieval Augmented Generation) settings and indexes per user.

`rag_enabled` defaults to `true` as of 2026-04. Existing profiles created before that are migrated once on load (watermarked by `opt_in.rag_migrated`). To disable, set `rag_enabled=false` via this API or the frontend settings UI — the watermark prevents re-flipping.

Conversation indexing happens automatically in four places: per turn, before `/compact` (manual + auto), before `/threads/{id}/clear`, and thread chunks are removed as part of the full `DELETE /threads/{id}` cascade. Saved profile memories are also synced into the memory chunk index when created or updated through the REST API or agent tools, and removed from the index when forgotten. See `Nymeria/docs/architecture.md` → "RAG (Semantic Conversation Recall)".

### Get RAG Settings

```http
GET /users/{user_id}/rag/settings
Authorization: Bearer <token>
```

**Response:**
```json
{
  "user_id": "default",
  "rag_enabled": true,
  "preferences": {
    "max_chunks": 5,
    "include_conversations": true,
    "include_memories": true,
    "include_todos": true,
    "auto_flush": true
  }
}
```

---

### Update RAG Settings

```http
PUT /users/{user_id}/rag/settings
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "rag_enabled": true,
  "max_chunks": 5,
  "include_conversations": true,
  "include_memories": true,
  "include_todos": false,
  "auto_flush": true
}
```

**Response:** Updated settings object

---

### Get RAG Statistics

```http
GET /users/{user_id}/rag/stats
Authorization: Bearer <token>
```

**Response:**
```json
{
  "user_id": "default",
  "total_chunks": 156,
  "chunks_by_type": {
    "conversation": 120,
    "memory": 25,
    "todo": 11
  },
  "index_size_bytes": 524288,
  "last_indexed": "2026-02-02T10:00:00Z"
}
```

---

### Search RAG Index

```http
GET /users/{user_id}/rag/search?q=<query>&max_results=5
Authorization: Bearer <token>
```

Searches the user's enabled RAG content types and returns matching chunks.

**Response:**
```json
{
  "user_id": "default",
  "query": "supplier pricing",
  "results": [
    {
      "id": "chunk-id",
      "content": "Relevant conversation or memory text",
      "chunk_type": "conversation",
      "thread_id": "thread-id",
      "created_at": "2026-04-27T12:00:00",
      "metadata": {},
      "score": 0.87
    }
  ],
  "total": 1
}
```

---

### Trigger RAG Reindex

```http
POST /users/{user_id}/rag/reindex
Authorization: Bearer <token>
```

Rebuilds saved-memory chunks in the RAG index without deleting conversation or TODO chunks.

**Response:**
```json
{
  "status": "ok",
  "cleared_memory_chunks": 25,
  "indexed_memories": 25,
  "message": "Index rebuilt. New conversations will be indexed automatically."
}
```

---

### Delete RAG Index

```http
DELETE /users/{user_id}/rag/index
Authorization: Bearer <token>
```

Permanently deletes the user's RAG index.

**Response:**
```json
{
  "message": "RAG index deleted for user default"
}
```

---

## User Tool Preferences API

Manage per-user tool visibility and configuration.

### List User Tools

```http
GET /users/{user_id}/tools
Authorization: Bearer <token>
```

Returns the user's tool inventory and enabled state.

---

### Search User Tools

```http
GET /users/{user_id}/tools/search?query=browser&category=&thread_id=thread-123&top_k=10&include_status=true
Authorization: Bearer <token>
```

Searches the user's visible tool catalog with the backend ranking service used
by the agent `tool_search` tool and chat-app command surfaces. The catalog is
role-filtered and can include thread-visible callable tools when `thread_id` is
provided.

**Query parameters:**
- `query` (`string`, default `""`): Search text.
- `category` (`string`, default `""`): Optional category filter.
- `thread_id` (`string`, optional): Thread context for callable tools and status annotations.
- `top_k` (`integer`, default `15`, max `50`): Maximum result count.
- `include_status` (`boolean`, default `true`): Include default/enabled/disabled status.

**Response:**
```json
{
  "query": "browser",
  "mode": "bm25",
  "warning": "semantic search unavailable (...); falling back to keyword search.",
  "results": [
    {
      "name": "browser_open",
      "description": "Open a browser page",
      "category": "browser",
      "security_level": "safe",
      "tool_type": "builtin",
      "is_default": false,
      "status": "available",
      "score": 2.31,
      "enable_hint": "/tools enable browser_open"
    }
  ]
}
```

`mode` is one of `semantic`, `bm25`, `fuzzy`, or `substring`. `warning` is
`null` when semantic search is active or no fallback warning is needed.

---

### Get Tool Preferences

```http
GET /users/{user_id}/tools/preferences
Authorization: Bearer <token>
```

Returns disabled tools plus per-tool config overrides.

---

### Configure Tool

```http
PUT /users/{user_id}/tools/{tool_name}/config
Content-Type: application/json
Authorization: Bearer <token>
```

Update config for a specific tool.

---

### Reset Tool Preferences

```http
POST /users/{user_id}/tools/reset
Authorization: Bearer <token>
```

Resets all tool preferences to defaults.

---

## Unified Tools API

Access built-in, MCP server, and admin-visible custom tools through one API
surface. This is also where default enablement, description overrides, and
tool configuration updates live.

### List Unified Tools

```http
GET /users/{user_id}/tools/unified
Authorization: Bearer <token>
```

Returns all visible tools in a unified format. Custom tool definitions include
HTTP/MCP configuration, so they are only returned to admin users.

### Enable Unified Tool

```http
PUT /users/{user_id}/tools/unified/{tool_id}/enable
Authorization: Bearer <token>
```

Enable or disable a built-in or MCP server tool through the unified tool
identity.

### Update Unified Tool Description

```http
PUT /users/{user_id}/tools/unified/{tool_id}/description
Authorization: Bearer <token>
```

Override the user-visible description for a tool.

### Update Unified Tool Config

```http
PUT /users/{user_id}/tools/unified/{tool_id}/config
Authorization: Bearer <token>
```

Update configuration for a unified tool entry.

**Response:**
```json
{
  "user_id": "default",
  "tools": [
    {
      "id": "bash_execute",
      "name": "bash_execute",
      "description": "Execute shell commands...",
      "type": "builtin",
      "category": "core",
      "enabled": true,
      "parameters": {...}
    },
    {
      "id": "get_weather",
      "name": "Get Weather",
      "description": "Get current weather...",
      "type": "custom",
      "category": "custom",
      "enabled": true,
      "parameters": {...}
    }
  ],
  "total": 48
}
```

---

### Enable Unified Tool

```http
PUT /users/{user_id}/tools/unified/{tool_id}/enable
Authorization: Bearer <token>
```

Works for both built-in and custom tools.

**Response:**
```json
{
  "message": "Tool 'get_weather' enabled"
}
```

---

### Update Tool Description

```http
PUT /users/{user_id}/tools/unified/{tool_id}/description
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "description": "Updated description for the tool"
}
```

**Response:**
```json
{
  "message": "Description updated for tool 'get_weather'"
}
```

---

### Configure Unified Tool

```http
PUT /users/{user_id}/tools/unified/{tool_id}/config
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{
  "timeout_seconds": 30,
  "custom_option": "value"
}
```

**Response:**
```json
{
  "message": "Tool 'get_weather' configured",
  "config": {...}
}
```

---

## MCP Servers, Devices, and Voice

### MCP Servers

```http
GET /mcp-servers
POST /mcp-servers
GET /mcp-servers/{server_id}
PUT /mcp-servers/{server_id}
DELETE /mcp-servers/{server_id}
POST /mcp-servers/install/preview
POST /mcp-servers/install/preview-upload
POST /mcp-servers/install
POST /mcp-servers/{server_id}/retry
POST /mcp-servers/{server_id}/discover
POST /mcp-servers/{server_id}/test
Authorization: Bearer <token>
```

Manage MCP server definitions, install from pasted sources, trigger tool discovery, and optionally auto-enable discovered MCP tools for a thread when creating a server. These handlers live in `Nymeria/nymeria/api/routers/mcp_servers.py`; request schemas live in `Nymeria/nymeria/api/schemas/mcp_servers.py`.

Paste install is now a preview-first flow:

- `POST /mcp-servers/install/preview` parses text without running anything. Accepted text includes Claude Desktop `mcpServers` JSON, bare stdio commands, HTTP/SSE URLs, npm package pages, PyPI package pages, GitHub/GitLab/Bitbucket repository URLs, bundle URLs, and registry ids.
- `POST /mcp-servers/install/preview-upload` accepts a multipart `file` field for `.mcpb`, `.dxt`, or `.zip` bundles and returns the same preview shape.
- `POST /mcp-servers/install` accepts either `source` or a `preview_token`. Safe package/HTTP installs can run after preview; Git/local-path/bundle installs require `confirmed: true`.
- `POST /mcp-servers/{server_id}/retry` reruns setup/discovery for a disabled draft or failed server.

Failed setup or discovery is not rolled back. Nymeria saves a disabled draft with `install_status`, `last_error`, `missing_config`, and `install_logs` so the frontend can show the failure and retry later. Sensitive install values are encrypted with `NYMERIA_SECRETS_KEY` before being written to disk.

Stdio commands run inside the Nymeria backend process environment. In Docker deployments, file paths must exist inside the `nymeria-api` container and services running on the host should usually be addressed with `host.docker.internal` instead of `localhost`. If a stdio server exits during initialization, the error detail includes its recent stderr output to expose path, import, or dependency failures.

Preview response:

```json
{
  "preview_token": "4f9f...",
  "server": {"id": "fetch-a1b2c3", "name": "fetch", "install_status": "ready"},
  "plan": {
    "source_type": "pypi",
    "runtime_type": "uvx",
    "risk_level": "low",
    "confirmation_required": false,
    "parsed_summary": "stdio MCP server 'fetch' running: uvx mcp-server-fetch",
    "command_preview": "uvx mcp-server-fetch",
    "warnings": [],
    "required_config": []
  }
}
```

Install request:

```json
{
  "preview_token": "4f9f...",
  "confirmed": true,
  "config_values": {"API_KEY": "..."},
  "auto_enable": true,
  "thread_id": "optional-thread-id"
}
```

### Device Registration

```http
POST /devices/register
DELETE /devices/{token}
Authorization: Bearer <token>
```

Register or unregister FCM device tokens for push notifications.
Registration binds the token to the authenticated caller; any `user_id` value
in the request body is ignored.

### Voice Endpoints

```http
POST /voice/chat
POST /voice/tts
POST /voice/stt
Authorization: Bearer <token>
```

- `/voice/chat` accepts audio upload, runs STT -> agent -> TTS, and returns audio.
- `/voice/tts` accepts JSON text and returns synthesized audio.
- `/voice/stt` accepts audio and returns transcribed text.

## Tool Categories API

### List Tool Categories

```http
GET /tools/categories
Authorization: Bearer <token>
```

Returns available tool categories.

**Response:**
```json
{
  "categories": {
    "core": ["bash_execute", "file_read", "file_write", "web_search", "consult", "notify"],
    "profile": ["memory_add", "memory_edit", "memory_read", "personality_set", "rag_search"],
    "todo": ["nym_todo", "nym_todo_delete", "nym_todo_list"],
    "skills": ["skill_manage", "list_installed_skills", "search_skills", "install_skill"],
    "mcp_server": ["manage_mcp", "search_mcp", "install_mcp_server"],
    "custom": ["tool_search", "tool_enable", "api_discover", "http_request", "tool_create", "skill_config", "skill_kit_create"],
    "self_modify": ["self_modify_rollback", "reload_all"],
    "thread_spawn": ["spawn_thread"]
  }
}
```

Capability-expansion categories are optional by default; the bundled
`self-improve` Skill Kit binds the consolidated facades when needed.

---

## Triggers API

Event-driven automations that fire actions when source conditions are met.

### List Sources

```http
GET /triggers/sources/list
Authorization: Bearer <token>
```

Returns all registered trigger sources with metadata.

**Response:**
```json
{
  "sources": {
    "webhook": {
      "name": "webhook",
      "description": "Fires when an HTTP POST is received",
      "config_schema": { ... },
      "category": "custom",
      "icon": "bolt",
      "setup_guide": "...",
      "template_variables": ["fired_at", "source_ip"],
      "example_config": {"secret": "my-shared-secret"},
      "requires_auth": null
    }
  }
}
```

### Reload Sources

```http
POST /triggers/sources/reload
Authorization: Bearer <token>
```

Reloads trigger source plugins from disk (e.g. after self-modify adds a new source). Returns `{"sources_loaded": <count>}`.

### Create Trigger

```http
POST /triggers
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "name": "RSS Monitor",
  "source_type": "rss",
  "source_config": { "url": "https://example.com/feed.xml" },
  "action_type": "agent_prompt",
  "action_config": { "prompt_template": "New article: {title} - {summary}" },
  "conditions": [{ "field": "title", "operator": "contains", "value": "release" }],
  "cooldown_seconds": 60,
  "enabled": true
}
```

### List Triggers

```http
GET /triggers?enabled_only=false&thread_id=<optional>
Authorization: Bearer <token>
```

**Query parameters:**
- `enabled_only` (`bool`, default `false`) — return only enabled triggers
- `thread_id` (`str`, optional) — filter to triggers whose action targets the given thread

### Get Trigger

```http
GET /triggers/{trigger_id}
Authorization: Bearer <token>
```

Returns a single trigger by ID. 404 if it does not exist for the authenticated user.

### Update Trigger

```http
PATCH /triggers/{trigger_id}
Authorization: Bearer <token>
Content-Type: application/json
```

Accepts any subset of: `name`, `enabled`, `source_config`, `action_type`, `action_config`, `conditions`, `cooldown_seconds`.

### Delete Trigger

```http
DELETE /triggers/{trigger_id}
Authorization: Bearer <token>
```

### Test Trigger (Dry Run)

```http
POST /triggers/{trigger_id}/test
Authorization: Bearer <token>
```

Returns a preview using sample event data without actually executing.

**Response:**
```json
{
  "sample_event": { "title": "...", "link": "..." },
  "rendered_output": "New article: ...",
  "action_type": "agent_prompt",
  "template_variables_used": ["title", "summary"],
  "conditions_pass": true
}
```

### Trigger Execution History

```http
GET /triggers/{trigger_id}/executions?limit=50
Authorization: Bearer <token>
```

Per-trigger execution history.

```http
GET /triggers/executions/recent?limit=50
Authorization: Bearer <token>
```

All recent executions across triggers.

### Fire Webhook

```http
POST /triggers/fire/{trigger_id}?secret=<shared-secret>
Content-Type: application/json
```

Push-based endpoint for webhook triggers. Accepts any JSON body. Public callers
must include the trigger's shared `secret` query parameter; alternatively,
include `Authorization: Bearer <token>` to fire the authenticated user's own
webhook trigger without putting the shared secret in the URL.

---

## Error Responses

All errors follow this format:

```json
{
  "detail": "Error message here"
}
```

| Status Code | Meaning |
|-------------|---------|
| 401 | Missing or invalid API key |
| 422 | Invalid request body |
| 500 | Internal server error |

---

## Hosted Frontend

When a built frontend is present at `nymeria/frontend/index.html` inside the
installed Python package, the API serves it at `GET /`. Source checkouts still
fall back to `Nymeria/frontend/index.html` for development bundles. Browser
navigation to unmatched non-API paths also returns `index.html` so the SPA can
handle refreshes and deep links.

Frontend routes are registered only when `index.html` exists. API routers are
registered first, so concrete API routes keep priority; there is no `/api/`
prefix migration in the beta routing plan.

In this backend-served browser mode, the frontend setup wizard probes same-origin
`/health` and fills the API URL with the current page origin when the health
JSON validates. Users still need to paste and test an account token.

Root-level build assets such as icons, manifests, and images are served from
the same frontend directory. Unmatched paths that look like API routes, missing
static assets, or requests that do not accept `text/html` still return `404`;
only browser-style HTML navigations receive the SPA fallback.

---

## CORS

CORS is controlled by `CORS_ORIGINS` in environment settings. The default
allows local desktop development and the bundled web UI:
`http://localhost:1420,tauri://localhost,http://tauri.localhost,https://tauri.localhost,http://localhost:8000`.
The `tauri.localhost` origins cover installed Tauri desktop builds, including
Windows. Add exact LAN or production frontend origins as needed. Wildcard
origins are rejected because the API allows credentialed CORS requests.

---

## Interactive Documentation

Interactive documentation is disabled by default. In trusted local development,
set `NYMERIA_API_DOCS=true` or `NYMERIA_DEBUG=true` and restart the API server:
- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc
- **OpenAPI schema:** http://localhost:8000/openapi.json
