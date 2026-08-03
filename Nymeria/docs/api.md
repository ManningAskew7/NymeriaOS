# Nymeria REST API

Base URL: `http://localhost:8000`

Implementation note: `create_api_app()` remains the public FastAPI factory.
The API is being split incrementally; the System slice (`/health`, `/ready`,
`/restart`, `/report`), device, workspace, RAG, user memory, user
tool-preference, Skills, voice, Agent Threads, activity/notification, TODO
dashboard, autonomous stream, custom tools, classic tool discovery/default/
callable routes, unified tools, MCP server management, settings/model catalog,
thread config/callable-team routes, chat-app/BYO Telegram routes, command
routes, and Chat SSE routes now live under `Nymeria/nymeria/api/routers/`.
Application construction, shared dependency injection, auth helpers, router
registration, and hosted-frontend/static fallback remain in
`Nymeria/nymeria/triggers/api.py`.

## Authentication

Most endpoints require Bearer token authentication:

```
Authorization: Bearer <token>
```

Per-user account tokens (`nym_<32-url-safe>`) are the only accepted bearer.
Create a first token with `python3 run.py users add`, or issue another token
for an existing user with `python3 run.py users issue-token` - see
`docs/accounts.md`. Tokens resolve to the user they were issued to.

`X-Nymeria-Act-As: <user_id>` is honored only for admin-role callers and rewrites the effective user to the target (403 for non-admin, 404 for unknown/disabled target).

Failed bearer authentication attempts are rate-limited per client IP. After 10 missing, malformed, or invalid tokens within 60 seconds, further failed attempts return `429` with a `Retry-After` header. Valid tokens are not blocked by this failed-auth counter.

Every API response includes `X-Request-ID`. If the client sends a safe
`X-Request-ID` header, Nymeria propagates it; otherwise Nymeria generates one
and includes it in request-scoped logs.

Exceptions without Bearer auth:
- `GET /health`
- `GET /health/stream`
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

### Health Stream (SSE probe)

```http
GET /health/stream
```

No authentication required. Emits three small SSE events spaced about 0.6s
apart, then an `event: end` and the stream closes. Exists so tunnels and
reverse proxies can be verified end to end for streaming, not just request
relay: a relay that buffers SSE delivers all events in one burst at close,
which the setup wizard's public-URL check detects (Cloudflare quick tunnels,
for example, pass `/health` but cannot carry SSE).

**Response** (`text/event-stream`):
```
data: {"seq": 0, "ts": "2026-06-10T00:00:00+00:00"}

data: {"seq": 1, "ts": "2026-06-10T00:00:00.6+00:00"}

data: {"seq": 2, "ts": "2026-06-10T00:00:01.2+00:00"}

event: end
data: {}
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

### Restart API Server

```http
POST /restart
Authorization: Bearer <admin-token>
```

Admin-only. Requests a restart of the API process and returns
`{"message": "Server restarting..."}` before the process exits.

---

### Report Problem

```http
POST /report
Content-Type: application/json
Authorization: Bearer <token>
```

Sends a support email with optional thread/message identifiers, a description,
client info, and up to the latest 10 included messages. The backend sends the
report through the configured error-report email destination.

**Request Body:**
```json
{
  "thread_id": "optional-thread-id",
  "message_id": "optional-message-id",
  "description": "What went wrong",
  "messages": [],
  "timestamp": "2026-05-20T12:00:00Z",
  "client_info": {"platform": "desktop"}
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
| `provider` | `discord`, `telegram`, `slack`, `whatsapp`, `teams` |
| `provider_user_id` | Platform-native user ID (string) |

**Responses:**
- `200`  -  `{"user_id": "bob"}`
- `400`  -  `{"detail": "Unknown provider"}`
- `403`  -  `{"detail": "Admin only"}` (non-admin token)
- `404`  -  `{"detail": "Not linked"}`

Create mappings via `POST /admin/users/{user_id}/platforms` (see Account & User Administration below).

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
POST /credential-prompts/{prompt_id}/test
GET /credential-prompts/{prompt_id}/status
POST /credential-prompts/{prompt_id}/submit
POST /credential-prompts/{prompt_id}/exit
POST /credential-prompts/{prompt_id}/cancel
Authorization: Bearer <token>
```

Stores reusable Nymeria tool/MCP/custom HTTP credentials encrypted with
`NYMERIA_SECRETS_KEY`. Responses return metadata, allowed targets, scopes,
status, and secret field names only. Plaintext is accepted only in write-only
`secret_fields` request properties and is never echoed.

Credential references use `${credential:<credential_id>.<field>}` in runtime
config. The backend resolves them inside MCP/custom HTTP execution after target
scope checks.

`request_credential` uses the credential prompt routes above for desktop modal
resolution. Text-chat setup links use separate prompt-token routes:

```http
GET /connect/credentials/{prompt_id}
GET /connect/credentials/oauth/callback
GET /connect/credentials/{prompt_id}/prompt
POST /connect/credentials/{prompt_id}/test
POST /connect/credentials/{prompt_id}/submit
POST /connect/credentials/{prompt_id}/exit
POST /connect/credentials/{prompt_id}/cancel
Authorization: Bearer <prompt-token>
```

The setup link is `${NYMERIA_PUBLIC_URL}/connect/credentials/{prompt_id}#<token>`.
Only the hosted form shell is unauthenticated; metadata, test, submit, exit,
and cancel require the one-time prompt token from the URL fragment.

See [`credentials.md`](credentials.md) for the storage model and migration notes.

---

### Account & User Administration

Self endpoints (`/me/*`) work for any authenticated user. Admin endpoints (`/admin/users/*`) require an admin token. Raw tokens are returned **once** in the response of any creation/issue/rotate call  -  they cannot be retrieved later.

Token revoke endpoints address tokens by `token_hash_prefix` (the first 8 hex chars of the sha256, returned in token list responses). The frontend never sees raw token material for tokens it didn't just mint.

**Self  -  any authenticated user:**

| Method | Path | Body | Notes |
|---|---|---|---|
| `PATCH` | `/me` | `{display_name?}` | Update your own display name. Empty value → 400. |
| `GET` | `/me/tokens` |  -  | List your tokens (no raw values). |
| `POST` | `/me/tokens` | `{label?}` | Issue yourself a token. Returns `{raw_token, metadata}`. |
| `DELETE` | `/me/tokens/{token_hash_prefix}` |  -  | Revoke. 400 on ambiguous prefix, 404 on no match. |

**Admin  -  caller must be admin:**

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/admin/users` |  -  | List every user with `token_count` and `last_token_use`. |
| `POST` | `/admin/users` | `{email, display_name?, role?, id?, token_label?}` | Create + issue first token. Returns `IssuedTokenResponse`. |
| `GET` | `/admin/users/{user_id}` |  -  | Single user with `thread_count`, `todo_count`, `platform_count`. |
| `PATCH` | `/admin/users/{user_id}` | `{display_name?, role?, disabled?}` | 409 if it would leave zero enabled admins. |
| `DELETE` | `/admin/users/{user_id}` |  -  | 409 if user owns threads or todos; clean those first. |
| `GET` | `/admin/users/{user_id}/tokens` |  -  | List a user's tokens. |
| `POST` | `/admin/users/{user_id}/tokens` | `{label?}` | Issue a token for the user. |
| `POST` | `/admin/users/{user_id}/tokens/rotate` | `{label?}` | Revoke all + issue one. Returns `RotatedTokensResponse` with `revoked_count`. |
| `DELETE` | `/admin/users/{user_id}/tokens/{token_hash_prefix}` |  -  | Revoke single by hash prefix. |
| `GET` | `/admin/users/{user_id}/platforms` |  -  | List chat-platform identities. |
| `POST` | `/admin/users/{user_id}/platforms` | `{provider, provider_user_id}` | Link. 409 if already owned by another user. |
| `DELETE` | `/admin/users/{user_id}/platforms/{provider}/{provider_user_id}` |  -  | Unlink. |

**Response shapes** (Pydantic models for this account surface live in
`Nymeria/nymeria/api/schemas/accounts.py`; other extracted schemas such as
System, Settings, Skills, Agent Threads, TODOs, MCP servers, Chat App/BYO
Telegram, and dashboard activity/notifications live under
`Nymeria/nymeria/api/schemas/`):

```jsonc
// IssuedTokenResponse  -  returned by POST /me/tokens, POST /admin/users,
// POST /admin/users/{user_id}/tokens. Raw token shown ONCE; the metadata.token_hash_prefix
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

// RotatedTokensResponse  -  returned by POST /admin/users/{user_id}/tokens/rotate.
// Same as IssuedTokenResponse plus the count of tokens that were just revoked.
{
  "raw_token": "nym_...",
  "metadata": { /* TokenInfo */ },
  "revoked_count": 3
}

// TokenInfo  -  returned by GET /me/tokens and GET /admin/users/{user_id}/tokens.
// No raw_token field  -  that's only ever in the issue/rotate response.
{
  "token_hash_prefix": "a3f9b1c2",
  "label": "iPhone",
  "created_at": "2026-04-25T10:00:00+00:00",
  "last_used_at": "2026-04-25T11:42:13+00:00",
  "revoked_at": null
}

// AdminUserResponse  -  returned by GET /admin/users (as a list) and
// GET /admin/users/{user_id}. The thread/todo/platform counts are present on
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

// PlatformIdentityResponse  -  returned by GET /admin/users/{user_id}/platforms (as a list)
// and POST /admin/users/{user_id}/platforms (single).
{
  "provider": "discord",
  "provider_user_id": "123456789012345678",
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
# Issue an admin token for these calls (see `accounts.md` for the account model)
TOKEN=$(python3 run.py users issue-token default --label admin-curl)

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

**Self-service  -  any authenticated user:**

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/me/platforms` |  -  | List linked chat-platform identities for the caller. |
| `POST` | `/me/platform-link-codes` | `{provider:"telegram\|slack\|whatsapp\|teams"}` | Issue a 10-minute self-link code. Telegram responses include an optional `t.me` deep link; shared bot providers use the raw `link <code>` command. |
| `POST` | `/threads/{thread_id}/chatapp/bind-code` | `{provider:"telegram\|slack\|whatsapp\|teams"}` | Issue a 10-minute thread-bind code. Caller must own the thread and the thread must not already be bound. |
| `GET` | `/threads/{thread_id}/chatapp/bindings` |  -  | List chat-app bindings for a thread the caller owns. |
| `DELETE` | `/threads/{thread_id}/chatapp/bindings/{binding_id}` |  -  | Delete a binding owned by the caller and emit sidebar platform sync. |
| `GET` | `/me/telegram-bots` |  -  | List user-owned Telegram bots without token material. |
| `POST` | `/me/telegram-bots` | `{bot_token}` | Validate via Telegram `getMe`, encrypt with `NYMERIA_SECRETS_KEY`, and register idempotently for the same owner. |
| `GET` | `/me/telegram-bots/{bot_id}` |  -  | Fetch one owned bot; used by the wizard while waiting for supervisor heartbeat. |
| `DELETE` | `/me/telegram-bots/{bot_id}` |  -  | Delete an owned bot and cascade-delete bindings served by that bot. |

**Bot/admin  -  caller must be admin:**

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/admin/chatapp/bindings` |  -  | List all bindings, optionally filtered by `?provider=telegram`, `slack`, `whatsapp`, or `teams`. |
| `GET` | `/admin/chatapp/bindings/lookup` |  -  | Resolve by exactly one of `platform_chat_id` or `thread_id`. |
| `POST` | `/admin/chatapp/bindings/claim` | `{code, provider, platform_chat_id, expected_provider_user_id}` | Shared-bot bind-code claim; verifies the platform user is linked to the issuing Nymeria user before consuming the code. |
| `POST` | `/admin/chatapp/bindings/claim-via-bot` | `{code, provider, platform_chat_id, via_user_telegram_bot_id}` | User-owned bot bind-code claim; authorizes by bot owner instead of platform identity. |
| `DELETE` | `/admin/chatapp/bindings/by-chat` |  -  | Remove a binding by `provider` and `platform_chat_id`; optional `user_id` and `user_telegram_bot_id` query params scope bot-initiated deletes to the expected owner/bot. |
| `POST` | `/admin/chatapp/bindings/switch` | `{provider, platform_chat_id, thread_id, user_id, user_telegram_bot_id?}` | Move a chat-app binding to another existing user-owned non-native thread. |
| `POST` | `/admin/platform/link-codes/claim` | `{code, provider, platform_user_id}` | Consume a self-link code and create the platform identity row. |
| `GET` | `/admin/telegram-bots` |  -  | Supervisor-only list of enabled user-owned bots with decrypted tokens. |
| `POST` | `/admin/telegram-bots/{bot_id}/seen` |  -  | Supervisor heartbeat update for `last_seen_at`. |

Bind-code claim routes inspect and authorize before consuming a code. If
authorization or binding creation fails, the code remains reusable until it
expires; successful claims consume it.

See [`telegram-bot.md`](chat-apps/telegram-bot.md), [`slack-bot.md`](chat-apps/slack-bot.md),
[`whatsapp-bot.md`](chat-apps/whatsapp-bot.md), and
[`teams-bot.md`](chat-apps/teams-bot.md)
for client-specific commands and setup behavior.

**WhatsApp Cloud API webhook:**

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/integrations/whatsapp/webhook` | Meta challenge token | Verifies Meta webhook setup with `hub.mode`, `hub.verify_token`, and `hub.challenge`; `hub.verify_token` must match `WHATSAPP_WEBHOOK_VERIFY_TOKEN`. |
| `POST` | `/integrations/whatsapp/webhook` | Required Meta signature | Accepts WhatsApp Cloud API message payloads. Requires `WHATSAPP_APP_SECRET` and a valid `X-Hub-Signature-256`; inbound message timestamps must be fresh before background processing and Graph API replies. |

**Microsoft Teams Bot Framework webhook:**

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/integrations/teams/webhook` | Bot Framework bearer token | Accepts Teams Bot Framework `message` activities. Always validates the connector JWT, then processes messages in the background and replies through the Bot Connector REST API. |

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
| `user_id` | string | No | `"default"` | Legacy compatibility field. The backend ignores client-claimed user IDs and uses the bearer token or admin `X-Nymeria-Act-As` as the effective user. |
| `attachments` | array | No | - | Optional multimodal attachments (images/documents) |
| `force_unsupported_attachments` | bool | No | `false` | Send request even if model modality checks fail |
| `is_self_invoke` | bool | No | `false` | Mark invocation as autonomous/internal. Skips the `message_added` sync event, routes the request through the autonomous prompt path, and mirrors supported stream events (`task_started`, `tool_call_delta`, `tool_call`, `tool_result`, `workspace_artifact`, `tool_reload`, `provider_retry`, `provider_fallback`, `image_input_unsupported`, `output_truncated`, `response_refused`, `turn_rewound`, `thinking`, `response`, `context_attached`, `compacting`, `compacted`, `iteration_limit`, `turn_resumed`, `task_completed`) to the autonomous event bus (visible via `GET /autonomous/stream`). Used by the Docker worker's relayed autonomous turns; gated by the same Bearer-auth check as any `/chat` call. |
| `trigger_override` | string | No | - | Label for autonomous invocations (e.g. `"watchdog"`, `"ticker"`). Becomes part of `task_id` and the `source` field on emitted autonomous events. |
| `trigger_id` | string | No | - | Trigger row ID for `trigger_override=="trigger"` calls. Surfaced on `task_started` and `task_completed` for frontend/bot classification. |
| `trigger_name` | string | No | - | Human-readable trigger name for `trigger_override=="trigger"` calls. Surfaced on `task_started` and `task_completed`. |
| `platform_origin` | object | No | - | Chat-bot provenance for the turn: `{platform, channel_id, message_id, kind}` (`kind` is `message` or `reaction`). Lets the `react` tool target the originating platform message and arms per-turn reply suppression; `kind="reaction"` also appends the react-tool guidance block to the prompt. Honored only when the authenticated caller is an admin or an admin acting-as (the bots and worker use the service token); silently ignored otherwise. A request without an honored origin clears the thread's recorded origin at turn start, so `react` only ever targets the current turn's message. Set by the Discord/Telegram bots; other callers omit it. |

Messages can start with a thread mention to route the turn to another thread:
`@ThreadName prompt`, `@thread-id-prefix prompt`, or `@"Thread With Spaces" prompt`.
The backend resolves the mention against threads visible to the authenticated
user by metadata title, callable name, or ID prefix, strips the mention before
invoking the agent, and persists the user message plus assistant response only
in the target thread. If the mention does not match any thread, the message is
treated as normal chat. If it matches multiple threads, `/chat` emits an `error`
event with
`code: "mention_ambiguous"` and candidate thread IDs.

**Capacity shedding (HTTP 429):** the API enforces an optional global ceiling
on concurrent interactive turns (`MAX_CONCURRENT_INTERACTIVE`, default `0` =
unlimited, feature off). When the ceiling is saturated, a request that would
start a new turn waits up to `INTERACTIVE_ADMISSION_WAIT_SECONDS` (default 10)
for a slot, then is rejected BEFORE the SSE handshake:

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 10

{"detail": "The server is at its interactive turn limit; your message was not started. Try again in a moment."}
```

`Retry-After` is advisory (the server cannot know when a running turn will
end). No `message_added` sync event is published for a shed request, so other
clients never render a user message that was not run. Exempt from the ceiling:
`is_self_invoke` relay turns (bounded by `MAX_CONCURRENT_AUTONOMOUS` on the
dispatch side instead) and prompts sent to a thread whose turn is already
running (those queue onto the running turn via `prompt_queued` and start no
new concurrency; a queued request that raced the thread lock releases its
admission slot as soon as `prompt_queued` is emitted). Disconnected holder
turns keep counting until they finish (they keep running and stay
re-attachable). The same contract applies to `POST /chat/sync`,
`POST /voice/chat`, and the in-process webhook-bot chat paths. This 429 is
distinct from the per-user request rate limit's 429 (`"Too many requests;
please slow down."`); both mean back off and retry.

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
data: {"type": "turn_started", "turn_id": "9f2c...", "thread_id": "abc123", "seq": 1}
data: {"type": "llm_call_started", "model": "claude-fable-5", "reasoning": true, "thread_id": "abc123", "seq": 2}
data: {"type": "thinking", "content": "...", "thread_id": "abc123", "seq": 3}
data: {"type": "response", "content": "I'll check that now.", "thread_id": "abc123", "seq": 4}
data: {"type": "tool_call_delta", "thread_id": "abc123", "seq": 5}
data: {"type": "tool_call", "name": "web_search_perplexity", "args": {...}, "thread_id": "abc123", "seq": 6}
data: {"type": "tool_result", "name": "web_search_perplexity", "result": "...", "thread_id": "abc123", "seq": 7}
data: {"type": "workspace_artifact", "tool_call_id": "tool1", "tool_name": "file_write", "path": "/workspace/report.csv", "name": "report.csv", "mime_type": "text/csv", "size_bytes": 1024, "thread_id": "abc123", "seq": 8}
data: {"type": "llm_call_started", "model": "claude-fable-5", "reasoning": true, "thread_id": "abc123", "seq": 9}
data: {"type": "response", "content": "...", "thread_id": "abc123", "seq": 10}
data: {"type": "done", "thread_id": "abc123", "seq": 11}
```

**`llm_call_started`** is a lightweight status event emitted the moment each
LLM call of the turn is dispatched to the provider (the first call and every
post-tool sub-turn call, as in the example above), before any token arrives.
`reasoning` tells the client whether the call's first output will be thinking
tokens (capability-gated: it also accounts for models that cannot disable
reasoning), so activity indicators can label the pre-first-token wait
honestly (for example suppressing a "writing a tool call" style guess when
the call will stream visible thinking first); `model` is the active
candidate (swap-aware across provider fallbacks). Clients that ignore the
event behave as before.

**Turn identity and re-attach:** when the request wins the thread lock and
becomes the executing (holder) turn, the stream opens with a `turn_started`
event carrying `turn_id`, and every subsequent event of that turn is stamped
with a monotonically increasing `seq`. Both fields are additive; clients that
ignore them behave as before. They exist for stream recovery: the backend
buffers the holder turn's events (bounded; last turn per thread, retained ~5
minutes after the turn ends), and a client whose connection dropped mid-turn
can replay and rejoin the live turn via
`GET /threads/{thread_id}/turn/stream` (see Threads). Requests that only
queue a prompt behind a running turn (`queued` / `prompt_queued` streams) are
not holder turns: they carry no `turn_started`/`seq`, and their recovery path
is re-attaching to the holder turn that absorbs the prompt.

Dispatched `@thread` messages first emit:

```
data: {"type": "dispatched", "thread_id": "caller123", "target_thread_id": "target456", "title": "Research", "dispatched_to": {"thread_id": "target456", "title": "Research", "original_thread_id": "caller123"}}
```

Subsequent stream events keep `thread_id` set to the caller thread for client
rendering and include `dispatched_to` metadata for the target. Clients use this
metadata to show a `Response from <thread>` reference line inside the same
assistant turn, not as a separate system message. In the desktop and mobile
clients that reference line is a clickable control that switches the active
thread to `target_thread_id`. The rest of the stream remains normal: `thinking`,
pre-tool `response` preamble, `tool_call`, `tool_result`, post-tool `response`,
artifacts, errors, and `done` all flow through the same rendering pipeline as a
non-dispatched chat turn. The target thread owns the persisted checkpoint
history.

**`/quick <prompt>`** reuses this exact dispatch contract. Instead of resolving
an existing thread, the backend mints a fresh, clean-context thread (id prefixed
`spawned-quick-`, no per-thread config so it runs on global defaults), runs the
prompt there, and re-tags the stream to the caller thread so the answer appears
inline without entering the caller thread's context. The fresh thread persists
as a temporary thread (idle-swept after ~24h if never continued, its clock reset
whenever it is used again), so the user can resume it later via `@<id>` or
`/thread switch <id>`. The quick thread's first response carries a trailing,
display-only `response` chunk with that continue hint (never written to any
checkpoint). `/quick` is registered with `execution_kind: "chat_stream"`, so
clients route it to `/chat` like `/skill`, `/kit`, and `/orchestrate`.

**`/done <prompt>`** is another chat_stream command intercepted here: it arms
a one-shot follow-up on the current thread. If a turn is running, the backend
creates a single-use thread-scoped `done` hook carrying the prompt and returns
an ack `response` + `done` (no turn runs; the prompt fires as a DONE
continuation when the running turn finishes, and the hook deletes itself). If
the thread is idle, the prompt simply runs as a normal turn. Excluded on
`is_self_invoke` turns. See `docs/agent-systems/hooks.md`.

**`/resume`** (chat_stream, intercepted on both `/chat` and `/chat/sync`)
truly resumes a turn that stopped at its iteration limit. A max-iterations
halt lands at the sub-turn boundary AFTER the crossing tool batch executed
(the `iteration_limit` event carries `resumable: true`), so the checkpoint
tail is clean: the resume re-drives the graph with no new message (the
compaction-resume shape) and the executed tool results drive the model's
next call. Nothing is added to model-visible history ("/resume" is never
recorded), and the turn-safety window is anchored at the resume point so the
continuation gets a fresh budget. The resumed turn is an ordinary holder turn
(SSE streaming, the turn replay buffer, re-attach, queued prompts, DONE hooks
all apply) and starts by emitting `turn_resumed`. A busy thread answers with
an error ack (a resume is never queued); a thread whose tail is not a
resumable halt answers with an `error` event, code `resume_invalid`
(streaming) or an explanation string (sync). Repeated-tool-loop halts are
deliberately not resumable. Excluded on `is_self_invoke` turns.

---

### Chat (Synchronous)

```http
POST /chat/sync
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** Same as the streaming endpoint, including the optional `is_self_invoke` and `trigger_override` fields. Self-invoke behavior here is identical to `/chat`  -  it's a pass-through to `agent.chat()` with those flags. The interactive capacity ceiling applies here too (HTTP 429 + `Retry-After`; see "Capacity shedding" under Chat (Streaming)).

**Response:**
```json
{
  "response": "Hello! How can I help?",
  "thread_id": "abc123",
  "suppress_reply": false
}
```

`suppress_reply` is `true` when the turn ran the `react` tool with
`suppress_reply=true` (only for requests that sent `platform_origin`); the
calling bot should post no reply text.

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

`turn` describes the thread's attachable holder-turn buffer (the current
turn, or the most recent one within its ~5-minute retention window):
`turn_id` matches the stream's `turn_started` event, `state` is one of
`live | done | error | aborted`, `last_seq` is the highest buffered `seq`,
`truncated` reports replay-buffer overflow, and `user_message_id` is the
graph message id of the turn's user message (the anchor viewers use to trim
hydrated history before replaying the turn; `null` for message-less `/resume`
turns). `holder_kind` is `user` (interactive) or `autonomous` (TODOs,
triggers, dreams, callables, spawns, watchdog), `source_label` is a
best-effort short label for the autonomous initiator, and
`user_message_internal` is true when the anchor is an internal autonomous
wakeup (hydrate history with `include_hidden_anchors=true` so the anchor
resolves even when the show-autonomous-prompts filter hides it). `null` when
nothing is attachable (no recent turn, retention expired, or an API restart,
which loses in-flight turns and their buffers).

**Response:**
```json
{
  "thread_id": "abc123",
  "revision": "1f07bcb2-1d7a-67d2-8003-65db7b8e71f9",
  "processing": false,
  "turn": {
    "turn_id": "9f2c8f6f2f0d4f0f8a3b1c2d3e4f5a6b",
    "state": "done",
    "last_seq": 42,
    "truncated": false,
    "user_message_id": "3f6e2b1a-8c4d-4e5f-9a0b-1c2d3e4f5a6b",
    "holder_kind": "user",
    "source_label": null,
    "user_message_internal": false
  }
}
```

---

### Re-attach to a Turn (Recovery and Live Watching)

```http
GET /threads/{thread_id}/turn/stream?turn_id=<id>&from_seq=<n>
Authorization: Bearer <token>
```

Recovery endpoint for dropped `POST /chat` streams, and the live-attach
endpoint for watching an in-flight turn from any client. The backend
deliberately keeps a turn running when its SSE client disconnects; this
endpoint replays the turn's buffered events (byte-identical to the original
stream, `seq` stamps included) and then tails live events until the turn
ends, so the client can re-render the full turn including the terminal `done`
that the original response suppressed after the disconnect.

The same replay-then-tail contract serves cross-client live attach: any
client with thread access (the owner on another device, or an admin) that
opens a thread while a holder turn is running can attach with `from_seq=0`
and watch the turn live. Viewer clients trim their hydrated history after
the turn's user-message anchor (the status `turn` block's `user_message_id`)
before replaying, so the persisted turn-so-far is not rendered twice. The
desktop and mobile GUIs do this automatically on thread open (desktop also
on its 5-second sync poll); the viewer's composer stays live in
compose-and-queue mode, and Stop/Resume work from the viewer like any other
client.

Autonomous turns buffer too: every locally-executed turn (scheduled TODOs,
triggers, dreams, callable invocations, spawned threads, the watchdog) and
every Docker-worker-relayed turn feeds the same per-thread buffer, so
watching an autonomous turn is the same attach flow (the status `turn` block
carries `holder_kind: "autonomous"` plus a `source_label`). Their anchor is
the internal wakeup prompt: viewers hydrate history with
`include_hidden_anchors=true`, which represents a filtered wakeup as an
invisible stub entry (`hidden: true`) carrying the anchor `message_id`, so
the trim contract is identical whether the per-thread
`show_autonomous_prompts` toggle renders the wakeup or hides it. The GUIs
prefer this attach path over the legacy client-side autonomous replay
whenever a live buffer exists. Not watchable: synchronous `/chat` turns (bot
platforms) and dispatched (`@thread`) turns.

Query parameters: `turn_id` (optional) pins the attach to the turn the client
was streaming (from its `turn_started` event); omit it to attach to the
thread's current or most recent turn. `from_seq` (optional, default 0)
replays only events with `seq` greater than the value; recovery clients
normally pass 0 and rebuild the whole assistant turn from the replay.

**Response:** SSE. The stream opens with a `turn_attach` meta event
(`turn_id`, `state`, `last_seq`, `truncated`, `user_message_id`,
`holder_kind`, `source_label`, `user_message_internal`), then the
replayed/live turn events follow. For a finished turn the stream ends after the last buffered
event; `state: "aborted"` means the turn died without a terminal event
(stop-cancelled turns instead carry their `error` event with
`code: "cancelled"`).

**Errors:** `404` `{"code": "turn_not_found"}` when there is nothing to
attach to (no recent turn, buffer expired or replaced by a newer turn, or an
API restart); `410` `{"code": "turn_replay_gap"}` when buffer overflow
evicted events after `from_seq`. A gap can also open MID-stream (overflow
eviction outrunning a slow reader): the stream then ends with a
`{"type": "turn_replay_gap"}` event instead of silently skipping the evicted
span. In all three cases clients fall back to history reconciliation: poll
`GET /threads/{id}/status` until `processing` is false, then reload
`GET /threads/{id}/history`.

Dispatched (`@thread`) turns are not re-attachable: their buffer is keyed to
the execution thread while the stream advertises the origin thread, so
recovery on the origin thread 404s and falls back to history reconciliation.

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
| `llm` | effective provider/model/API mode/thinking labels, secret-safe override flags, and a `reasoning_passback` object (see below) |
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

The `llm.reasoning_passback` object reports whether this thread's prior-turn
reasoning is replayed to the model (see
`docs/agent-systems/reasoning-streaming.md`):

| Field | Meaning |
|-------|---------|
| `mechanism` | `anthropic_thinking` / `responses_items` / `openrouter_reasoning_details` / `flat_reasoning_content` / `gemini_thought_signatures` / `bedrock_reasoning` / `none` |
| `mechanism_label` | human-readable mechanism string |
| `fidelity` | `signed` / `plaintext` / `none` |
| `scope` | `all_turns` / `tool_call_turns_only` / `none` |
| `reasoning_enabled` | whether the model will actually emit reasoning to replay |
| `status` | `not_applicable` / `dropped` / `wired` / `active` (active = confirmed on a real turn) |
| `verified` | provider's `reasoning_passback_verified` smoke-test marker |
| `caveats` | human notes (e.g. plaintext-only, tool-call turns only, dropped) |
| `last_confirmed_at` | epoch seconds of the last confirmed replay, or null |

---

### Get Thread History

```http
GET /threads/{thread_id}/history
Authorization: Bearer <token>
```

Optional query: `include_internal=true` returns system-generated messages (autonomous wake-ups and compact prompts) that are hidden by default. Compaction markers are visible by default as `system` messages with `kind: "compaction_notice"`. Notes about the effective config differing from the requested one appear as `system` messages with `kind: "fallback_notice"` (fields: `phase` `swap`|`end`|`rejected`, `note_kind` `refusal`|`transport`|`destination`, `from_model`, `to_model`, `reason`, and a renderable one-line `content`): the runtime explains a fallback swap, the end of a hold, or a per-thread `base_url` it refused to send the server's credential to, inside the conversation, and history strips that appended note from the user bubble and re-emits it as this typed entry. `note_kind` distinguishes them: `refusal`/`transport` are model swaps and carry `from_model`/`to_model`, while `destination` carries neither and must not be rendered as a model switch.

Optional query: `show_autonomous_prompts=true|false` overrides the per-thread `show_autonomous_prompts` config for this request only (without mutating thread state). When omitted, the backend falls back to the per-thread field. The desktop and mobile clients pass this query param based on their global "Show autonomous prompts" preference (combined with the per-thread force-on override), so a single global setting can drive history filtering without flipping every thread's config. MCP and CLI callers don't pass it and keep today's behavior. Ignored when `include_internal=true` (which always returns everything).

Optional query: `include_hidden_anchors=true` (default false) emits an invisible stub entry for each autonomous wakeup the `show_autonomous_prompts` filter would drop: `{"role": "user", "hidden": true, "content": "", "message_id": "<graph id>"}` in graph order, with the wakeup text never included. Live-attach viewers use the stubs to trim hydrated history at an autonomous turn's anchor (`user_message_id` in the status `turn` block) even when the wakeup is hidden; clients must render nothing for `hidden` entries and exclude them from rewind/edit targeting. The desktop and mobile GUIs always pass it. No effect when wakeups are visible or with `include_internal=true`.

**Response:**
```json
{
  "thread_id": "abc123",
  "messages": [
    {"id": "abc123-1", "role": "user", "content": "Hello", "message_id": "run-77af3c-0"},
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

User entries carry `message_id`, the underlying LangGraph message id, which is the exact-targeting handle for the rewind endpoint's `to_message_id` (the entry `id` is a synthetic per-render counter and is not stable across fetches).

Assistant `steps` are optional. They appear when a turn has reasoning/thinking, tool calls, or interleaved response chunks. Anthropic typed thinking blocks, OpenAI-compatible `reasoning_content` metadata, and OpenAI Responses `reasoning` summary blocks are returned as `{"type": "thinking"}` steps so desktop and mobile can re-render the same thinking dropdown after history sync. Visible assistant commentary before a tool call is not thinking; it is stored and served as a `{"type": "response"}` step before the `tool_call` step.

The final assistant message carries `"processing": true` when the thread is mid-turn (has an active agent lock) and the latest checkpoint message is assistant/tool output, i.e. that displayed turn is the one currently being generated. Clients use this to keep rendering the in-flight turn as a single streaming bubble when a thread is opened mid-stream, instead of finishing it and starting a new one. The flag is omitted when a new turn has only queued its (filtered) wake-up input, so the displayed tail is a prior completed reply and must not be reused.

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
  "turn_recorded": true,
  "turn_llm_seconds": 7.125,
  "tokens_per_second": 70.2,
  "cumulative_tokens": 512000,
  "context_limit": 200000,
  "usage_percentage": 22.5,
  "compaction_count": 1,
  "compact_trigger_tokens": 160000,
  "last_compaction": "2025-01-15T10:30:00Z",
  "context_management": "auto_compact",
  "cost_usd_last": 0.012345,
  "cost_usd_cumulative": 0.123456,
  "cost_unavailable": false
}
```

Token-field semantics (2026-07-04 repair pass):

- `total_tokens` is context occupancy: the final model call's prompt tokens,
  i.e. how full the window is. Compaction resets it; it is NOT a sum.
- `input_tokens` / `output_tokens` are the LAST TURN's consumption, summed
  across all of that turn's model calls (a multi-step tool turn counts every
  step). `turn_recorded: false` means usage extraction found nothing for the
  turn (the zeros are placeholders, clients must not accumulate them).
- `turn_llm_seconds` is the turn's LLM streaming wall time (excludes tool
  execution and retry backoff); `tokens_per_second` is
  `output_tokens / turn_llm_seconds` for the turn. Both are `null` when
  timing or tokens are unknown (e.g. right after a restart).
- `cumulative_tokens` is lifetime consumption (sum of turn input+output);
  it survives compaction.

`compact_trigger_tokens` is the resolved auto-compact trigger with per-thread
threshold overrides applied; it is `null` when `context_management` is not
`auto_compact`.

---

### Get Raw Checkpoint

```http
GET /threads/{thread_id}/checkpoint
Authorization: Bearer <token>
```

Developer/debugging endpoint. Returns the raw, deserialized latest LangGraph
checkpoint for a thread. Owner-only (same access rule as history). Unlike
`/history`, no display projection is applied: every message is returned verbatim
(tool calls, standalone tool results, metadata) so the persisted state can be
verified. Only the latest snapshot is returned, because its `messages` channel
already holds the full multi-turn history.

**Response:**
```json
{
  "thread_id": "abc123",
  "checkpoint_id": "1efabc...",
  "checkpoint_ns": "",
  "next": [],
  "config": { "configurable": { "thread_id": "abc123", "checkpoint_id": "1efabc..." } },
  "metadata": { "source": "loop", "step": 4 },
  "created_at": "2026-01-15T10:30:00+00:00",
  "parent_config": { "configurable": { "thread_id": "abc123", "checkpoint_id": "1efaaa..." } },
  "message_count": 6,
  "values": {
    "messages": [
      { "type": "human", "content": "..." },
      { "type": "ai", "content": "", "tool_calls": [ { "name": "web_search_perplexity", "args": {}, "id": "call_1" } ] },
      { "type": "tool", "content": "...", "tool_call_id": "call_1" }
    ]
  }
}
```

The desktop app surfaces this behind a client-local Developer mode toggle (a raw
checkpoint button in the thread header that previews or downloads this payload).

---

### Get Thread Platform Metadata

```http
GET /threads/{thread_id}/metadata
Authorization: Bearer <token>
```

Returns platform metadata parsed from the thread ID, such as Discord guild or
channel IDs, Telegram channel IDs, or `{"platform":
"desktop"}` for ordinary desktop/mobile/API threads.

---

### Thread Attachment Limits

```http
GET /threads/{thread_id}/attachment_limits
Authorization: Bearer <token>
```

Returns attachment caps for the thread's effective provider/model after
per-thread LLM overrides are applied.

**Response:**
```json
{
  "effective_provider": "openrouter",
  "effective_model": "anthropic/claude-sonnet-4",
  "limits": {
    "max_images_per_request": 100,
    "max_image_bytes": null,
    "max_pdf_pages": null,
    "max_total_bytes": null
  }
}
```

---

### Validate Thread Attachments

```http
POST /threads/{thread_id}/attachments/validate
Content-Type: application/json
Authorization: Bearer <token>
```

Preflights attachment compatibility against the thread's effective model. It
does not send content to a model.

**Request Body:**
```json
{
  "attachments": [
    {
      "file_type": "image",
      "data_url": "data:image/png;base64,...",
      "mime_type": "image/png",
      "file_name": "diagram.png"
    }
  ]
}
```

**Response:** includes `compatible`, effective provider/model, required and
unsupported modalities, warnings, `can_force_send`, and the same `limits`
object returned by `/attachment_limits`.

---

### Download Thread Attachment

```http
GET /threads/{thread_id}/attachments/{attachment_id}/download
Authorization: Bearer <token>
```

Streams original bytes from the thread's attachment sandbox. The caller must
own the thread or be an admin acting as that user.

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

### Clear Thread Conversation

```http
POST /threads/{thread_id}/clear
Authorization: Bearer <token>
```

Deletes checkpoint history for the thread and removes its metadata entry while
preserving thread config, notepad content, and other thread settings. Use
`DELETE /threads/{thread_id}` for full cascade deletion.

**Response:**
```json
{
  "status": "ok",
  "thread_id": "abc123"
}
```

---

### Compact Thread

```http
POST /threads/{thread_id}/compact?priority=<focus instruction>
Authorization: Bearer <token>
```

Manually trigger context compaction for a thread.

Optional `priority` query param: a free-text focus instruction that steers what the generated summary emphasizes (for example `keep the exact auth-flow decisions and failing test names`). It is normalized and capped at 1,000 chars. The framing is "prioritize, not filter": every required summary section and all durable-memory writes still happen; the focus only changes emphasis. Omit it for the default summary.

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

### Prune Thread Tool Returns

```http
POST /threads/{thread_id}/prune?mode=full
Authorization: Bearer <token>
```

Deterministically rewrites large tool-result messages in the active state
without an LLM call. It preserves message IDs and tool-call IDs so existing
assistant/tool linkage remains valid. The `mode` query param (default `full`)
selects the rewrite: `full` replaces each result body with a short placeholder;
`soft` keeps the first 500 characters and tags the truncation. The CLI exposes
both as `/prune [soft|full]`.

---

### Rewind Thread

```http
POST /threads/{thread_id}/rewind
Authorization: Bearer <token>
Content-Type: application/json

{ "steps": 1 }
```

Removes trailing user+assistant exchanges from the thread's message state.
An exchange starts at a `HumanMessage` and includes every following
`AIMessage` and `ToolMessage` up to the next `HumanMessage`. Uses LangGraph's
`RemoveMessage` + `update_state` (the same mechanism as context trimming),
so the message IDs and reducer history remain consistent.

Two addressing modes:

- `steps` (optional, defaults to `1`, between `1` and `100`): removes the
  last N exchanges. Backs the `/undo` and `/retry` slash commands in the
  CLI. If the thread has fewer than `steps` exchanges, all available cycles
  are removed.
- `to_message_id` (optional): the LangGraph id of the user message to rewind
  to, inclusive; that message and everything after it are removed. Takes
  precedence over `steps` when set. History user entries expose this id as
  `message_id`, and the desktop/mobile edit and rewind bubble affordances
  use it for exact targeting. Returns `404` when the id is not a user
  message in thread state (for example, removed by compaction).

The endpoint refuses with `409` while the thread lock is held (a turn is
running); stop the turn first. The guard is advisory: it probes the lock
just before rewinding rather than holding it through the rewind, so a turn
starting in the same instant can still race it (the same trade-off as the
stop endpoint). On success with `removed > 0` it publishes a
`thread_rewound` sync event on `/autonomous/stream` (see the sync-events
table); a no-op rewind (nothing to remove) returns `200` without an event.

**Response:**
```json
{
  "status": "ok",
  "thread_id": "abc123",
  "steps": 1,
  "removed": 2
}
```

`removed` is the number of underlying messages actually deleted, which is
usually larger than `steps` because each exchange contains a human turn plus
one or more assistant/tool messages. In `to_message_id` mode, `steps` echoes
the number of exchanges the rewind spanned.

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

**Keepalive comments:** during silent stretches (a long tool call emitting nothing), the chat stream sends an SSE comment frame `: keepalive` every 25 seconds. Comment lines (anything starting with `:`) carry no event and must be ignored, per the SSE spec; parsers that only act on `data: `-prefixed lines (all Nymeria clients) need no change. This keeps proxies and tunnel edges with idle timeouts (Cloudflare closes at ~100s) from cutting a turn mid-stream.

| Type | Description | Fields |
|------|-------------|--------|
| `llm_call_started` | Status-only: an LLM call was just dispatched to the provider (fires per call, including post-tool sub-turn calls), before any token arrives. Lets activity indicators label the pre-first-token wait honestly. | `model` (active candidate, fallback-swap-aware), `reasoning` (whether the call's first output will be thinking tokens) |
| `thinking` | Agent reasoning / extended-thinking text | `content` |
| `tool_call_delta` | Status-only hint that the model is streaming tool-call argument chunks before the tool starts | none |
| `tool_call` | Tool being invoked | `name`, `args`, `started_at` (server ISO-8601 UTC), `timeout_seconds` (max seconds before the backend terminates the call; render elapsed/max); on a managed MCP tool also `tool_type` (`mcp_server`), `server_id`, `server_name` (origin server for a provenance badge; the `name` stays the clean `mcp__server__tool`) |
| `tool_result` | Tool execution result | `name`, `result`, `started_at`, `duration_ms` (server-measured execution time); on a managed MCP tool also `tool_type`/`server_id`/`server_name` as on `tool_call` |
| `workspace_artifact` | Downloadable file generated by a tool (for example `file_write(..., attach=True)`) | `tool_call_id`, `tool_name`, `path`, `name`, `mime_type`, `size_bytes` |
| `reply_suppressed` | The `react` tool ran with `suppress_reply=true`; delivery surfaces for the turn's origin platform should drop buffered/subsequent reply text. Emitted right after the tool's `tool_result` (also when react runs via `tool_invoke`), and only when the backend registry confirms the real react call set the flag: marker text echoed by any other tool's output is inert | `tool_call_id` |
| `tool_reload` | Tool registry was reloaded mid-turn; resume metadata for next iteration | `tools`, `ttl`, `ttl_seconds`, `source`, `skill_name`, `reason` |
| `provider_retry` | Retryable provider/model failure; backend is sleeping before retry. If the provider failed after partial stream output, the backend rewound to the latest stable checkpoint first. | `provider`, `model`, `attempt`, `max_retries`, `delay_seconds`, `reason`, optional `http_status`, optional `rewound`, optional `stream_chunks` |
| `provider_fallback` | The backend switched to a configured fallback provider/model: primary retries were exhausted (may follow a post-stream rewind), or an empty provider refusal was swapped-and-re-run (`reason: refusal`, no retry loop). A consented switch may carry the user-chosen hold (`permanent: true` for an until-reverted hold). | `from_provider`, `from_model`, `to_provider`, `to_model`, `hold_seconds`, `expires_at`, optional `permanent`, `reason`, optional `http_status`, optional `rewound`, optional `stream_chunks` |
| `image_input_unsupported` | The model rejected image/PDF input (it cannot see attachments); the backend stripped the attachment (leaving a placeholder note in-context) and retried the turn once. The model is also recorded as image/file-incapable so later turns strip proactively. | `reason`, `model` |
| `output_truncated` | The turn hit its output-token cap. `produced_output` distinguishes the two shapes: `true` means the answer was cut off mid-generation, `false` means the cap was exhausted before any text or tool call was emitted (reasoning consumed the whole budget), in which case the backend attaches a visible note to the otherwise empty message so the turn cannot deliver silence. | `reason`, `produced_output`, `output_tokens`, `model` |
| `response_refused` | The provider ended the response with a refusal (Anthropic `stop_reason: refusal` / OpenAI `finish_reason: content_filter`). `produced_output: false` means it fired before any text or tool call (the model emitted at most a thinking block). `swapped: true` means the refusal-swap discarded the refused response and is re-running on the next fallback model (a `provider_fallback` with `reason: refusal` follows); `swapped: false` means the notice/rewind path handles it: the backend attaches a visible note to the otherwise empty message. Refusals are often phrasing-sensitive and intermittent, so rephrasing and retrying usually resolves them. | `produced_output`, `output_tokens`, `model`, `swapped` |
| `turn_rewound` | A pre-output refusal was rewound server-side: the backend removed the refused exchange from the checkpoint (refusals tend to repeat until the refused turn is reset), so the thread is back at the end of the previous turn. Fires ONLY when the refused exchange produced nothing besides the refusal: its user prompt(s) (a queued-prompt batch rewinds as one run, `prompt` joins the batch texts) followed directly by the refused message. An exchange that ran tools or delivered earlier output is never rewound (side effects already happened); it keeps the visible in-message note, which the stream also delivers as a trailing `response` chunk so live surfaces are not silent. Controlled clients should truncate their local transcript from `to_message_id` (the removed run's FIRST user-message anchor) and restore `prompt` to the composer, without auto-resending; clients without a composer (chat bots) deliver `content` as the turn's reply text. Follows `response_refused` (which stays pure telemetry). On a rewind failure this event is not emitted and the note-plus-response-chunk fallback applies. | `reason` (`refusal`), `removed`, `to_message_id`, `prompt`, `model`, `content` |
| `hook_activity` | A meaningful mutate-plane lifecycle-hook run (a deny/modify/inject or a fault); ephemeral live line only, nothing persisted. Only emitted while hooks are enabled and a mutate hook did something; the durable record is `GET /hooks/executions`. | `name`, `event`, `status`, `detail`, optional `tool_name` |
| `auth_prompt` | Credential setup prompt from `request_credential`; desktop opens the modal and chat bots render the secure setup link | `prompt_id`, `credential_id`, `provider`, `display_name`, `mode`, `fields`, `timeout_seconds`, optional `expires_at`, optional `connect_url`, `connect_url_required`, `connect_url_error`, optional OAuth fields such as `flow`, `auth_url`, `user_code`, `verification_uri`, `scopes` |
| `auth_prompt_resolved` | Credential prompt completed. Desktop keeps OAuth prompts open long enough to show success; non-OAuth prompts normally close from the submit action | `prompt_id`, `credential_id`, `status`, optional `message`, `email`, `name` |
| `auth_prompt_cancelled` | Credential prompt ended without an active credential, including user cancel, OAuth denial, expiry, or provider error | `prompt_id`, `reason`, optional `message` |
| `ui_prompt` | Interactive HTML form from the `ui_prompt` tool; the agent blocks until it resolves. The desktop app renders the agent-authored HTML in a sandboxed iframe modal (`sandbox="allow-scripts"`, null origin, a `default-src 'none'` CSP so no network request leaves the frame, all assets inlined) and POSTs the user's answer to `POST /ui-prompts/{prompt_id}/result` with `{"status": "submitted"\|"cancelled", "values": {...}}` (owner or admin; the first resolve wins, later POSTs get `delivered: false`). Other clients ignore the event and the tool times out | `prompt_id`, `title`, `html`, `timeout_seconds` (the effective wait: the tool's requested value clamped to 15-600 and capped just below the server's `tool_timeout`, so the countdown always matches the real deadline), `expires_at` |
| `ui_prompt_result` | A `ui_prompt` was resolved: a client answered or dismissed it, or the backend closed it (tool timeout, thread abort or turn cancellation, orphan sweep); every client retracts its copy of the modal. Deliberately never carries the submitted values | `prompt_id`, `status` (`submitted`/`cancelled` from a client resolve; `timeout`/`aborted`/`turn_cancelled`/`swept` from backend closures) |
| `hook_approval` | A `require_approval` hook is holding a tool call for the user's decision (resolve via `POST /hooks/approvals/{record_id}/resolve`, the tool-call card buttons, `/hook approve\|deny`, or the Telegram/Discord buttons); no answer by `expires_at` denies the call | `record_id`, `tool_call_id`, `tool_name`, `tool_args_preview`, `prompt`, `hook_id`, `hook_name`, `is_autonomous`, `created_at`, `expires_at` |
| `hook_approval_resolved` | A held tool call was resolved from any surface (or timed out, or its turn was aborted); every client retracts its approval prompt | `record_id`, `tool_call_id`, `tool_name`, `outcome` (`approved`/`denied`/`timeout`/`aborted`/`stale`), `resolved_by`, `note` |
| `fallback_prompt` | An `ask`-mode model switch parked the turn for the user's decision: swap to the fallback model or not (resolve via `POST /llm/fallback-approvals/{record_id}/resolve`, `/fallback approve\|deny`, the GUI/CLI consent cards, or the Telegram/Discord Swap buttons). `kind: transport` means the primary exhausted its retries on a provider error (decline fails the turn); `kind: refusal` means an empty provider refusal can re-run on the fallback (decline falls back to the rewind-and-restore path). No answer by `expires_at` AUTO-SWAPS (a fallback is a resilience action; the inverse of hook-approval timeout). Only a turn a human is watching parks (interactive source, holder kind `user`, an async streaming surface): autonomous turns, callable/handoff child turns, and `/chat/sync` callers swap immediately. Bot-origin turns park only on platforms whose bot renders inline consent buttons (Telegram, Discord); other bot platforms swap immediately, with `/fallback` as the management path | `record_id`, `kind`, `thread_id`, `from_provider`, `from_model`, `to_provider`, `to_model`, `reason`, optional `http_status`, `timeout_seconds`, `hold_options`, `allow_permanent`, `default_hold_seconds`, `is_autonomous`, `created_at`, `expires_at` |
| `fallback_prompt_resolved` | A parked model switch was resolved from any surface (or timed out, or its turn was aborted); every client retracts its consent prompt. `approved` carries the chosen hold when one was picked | `record_id`, `kind`, `outcome` (`approved`/`declined`/`timeout`/`aborted`/`stale`), `resolved_by`, optional `hold_seconds`, `hold_permanent`, `note` |
| `workflow_approval` | A workflow run suspended on `nym.approve`, awaiting the owner's decision (resolve via `POST /workflows/approvals/{record_id}/resolve`) | `record_id`, `workflow_id`, `prompt`, `expires_at` |
| `workflow_approval_resolved` | A suspended workflow run was approved, declined, or expired; the continuation ran (or was refused) | `record_id`, `workflow_id`, `approved`, `note`, `run_id` |
| `workflow_step` | One completed `nym.*` verb dispatch in a running workflow (live progress; best-effort and unordered, sort by `step`). Lean by design: args/result summaries live in the persisted run record, not on the wire | `workflow_id`, `run_id`, `step`, `verb`, `status`, `duration_ms`, optional `error_kind` |
| `workflow_run_finished` | A workflow run ended (fires for every run, including adhoc test runs that persist nothing) | `workflow_id`, `run_id`, `status` |
| `cli_config` | Agent-pushed CLI configuration command (`cli_statusbar_*` tools). User-scoped: every connected terminal CLI applies and persists it locally, then POSTs its outcome to `POST /cli-config/{command_id}/result`; the first ack resolves the awaiting tool. `script:` segment refs are rejected on this channel by both the tool and the client (script segments execute on the user's machine, so they are installed locally via `/statusbar` only). Non-CLI clients ignore the event. | `command_id`, `command_type` (`statusbar_get`/`statusbar_set`), `args`, `timeout_seconds` |
| `dispatched` | Leading `@thread` mention (or `/quick`) routed the turn to another thread; the answer streams inline on the caller thread | `target_thread_id`, `title`, `matched_ref`, `dispatched_to` |
| `response` | Visible assistant text chunk. May appear before a `tool_call` as preamble/commentary, or after tools as the final answer. | `content` |
| `context_attached` | Previous context summary attached to this message | `summary` |
| `compacting` | Context summary generation has started after the compaction path passes its start checks | `message` |
| `compacted` | Context was compacted; async streams may resume afterward | `messages_removed`, `auto_resumed`, `summary` |
| `queued` | Legacy: thread is busy with another turn; client should wait. Now emitted alongside `prompt_queued`; will be dropped once frontends adopt the new event. | `content`, `holder`, `held_seconds` |
| `prompt_queued` | Prompt was placed on the per-thread sub-turn queue because the thread was busy. The currently-running turn will halt at its next sub-turn boundary and absorb the queued prompt. | `position`, `holder`, `held_seconds`, `source` |
| `turn_halted` | The running turn observed pending queued prompts and ended early at the post-tools boundary; the drain loop is about to inject the queued prompts. | `reason`, `count` |
| `prompt_injected` | One or more queued prompts have been turned into HumanMessages and appended to the checkpoint; the graph is being re-driven to absorb them. `prompts` carries the queued texts (`{text, source_label, user_id, enqueued_at}`, index-parallel with `sources`) so clients that did not enqueue locally (live-attach viewers, other same-user clients) can render the injected user bubbles. | `count`, `sources`, `prompts` |
| `prompt_absorbed` | The queuer's specific prompt finished being absorbed. Mirrors the holder's full event stream to the queuer's connection in real time leading up to this. | `thread_id` |
| `fanout_dropped` | The queuer's fanout mailbox overflowed its bound (slow consumer); some events were dropped from this queuer's mirror. | `dropped_count` |
| `iteration_limit` | Agent hit a turn safety stop: either the max tool-call budget or repeated same tool/args/result loop detection. A max-iterations halt lands at the sub-turn boundary AFTER the crossing tool batch executes, leaving a clean resumable tail. | `content`, `reason`, `scope` (`main_agent`/`sub_agent`), `max_iterations`, `tool_call_count`, `resumable` (true only for graceful main-agent cap halts; gates the client Resume affordance), optional `repeated_tool_name`, `repeated_count`, optional `agent_name` (sub-agent scope) |
| `turn_resumed` | A `/resume` re-drive of a halted turn started. Emitted before the continuation streams; other clients watching the thread receive it via the autonomous mirror and can flip their pause card. | `tool_call_offset` (the tool-call count anchored away so the continuation gets a fresh safety window) |
| `error` | Error message | `content`, optional `code` (e.g. `aborted`, `cancelled` (turn aborted via the stop endpoint; the client should finalize its stop UI on this frame), `restored` (a queued prompt was handed back unprocessed because the user stopped the turn; the queuer's stream ends with this instead of `prompt_absorbed`), `queue_attachments_unsupported`, `cross_user_queue_unsupported`, `queue_overflow`) |
| `done` | Stream complete | `context_stats` (same shape as `GET /threads/{id}/context`, including the per-turn `input_tokens`/`output_tokens`/`turn_recorded`/`turn_llm_seconds`/`tokens_per_second` fields), `model` (when available), `suppress_reply: true` when the turn requested reply suppression (only for requests that sent `platform_origin`) |

All events include `thread_id` for correlation.

### Sub-Turn Steering (v1 same-process scope)

When a new prompt (user chat, callable thread, MCP, watchdog sweep, trigger,
or scheduled TODO) arrives while a thread is mid-turn, it is queued
per-thread; the running turn observes the queue at every
post-tools boundary in its ReAct loop and halts to absorb the
queued prompts. `astream()` then drains the queue, builds one
`HumanMessage` per pending prompt (with a metadata header naming the
source and arrival time), `aupdate_state`s them into the checkpoint,
and re-drives the graph so the model sees the new context without
re-entering the entrypoint or losing tool state.

Interactive queuers (user chat, callable threads, MCP) attach a
cross-loop-safe mailbox to their pending entry; the holder's stream
events fan out to that mailbox, so a queuer's SSE connection sees the
holder's response in real time once injection happens. The queuer's
own stream emits `prompt_queued` first, then mirrors holder events,
and ends with `prompt_absorbed`. Fire-and-forget queuers (triggers,
ticker, watchdog sweep) wait on a `threading.Event` and skip the mailbox.

Queued prompts with attachments/images are rejected at enqueue time
with `error.code=queue_attachments_unsupported` (the queue bypasses
`prepare_astream_input` which is where multimodal compatibility lives
today; v2 may revisit). Aborting a thread with `POST /threads/{id}/stop`
clears the queue and wakes blocked queuers with `error.code=aborted`.
The process-local FIFO is bounded to 256 prompts per thread; if it
overflows, the oldest blocked queuer is woken with
`error.code=queue_overflow`. If a prompt arrives while the holder is
past its final drain point and releasing the lock, it is not queued:
the caller waits for the lock and runs as the next normal turn.

Queued prompts are only absorbed into a holder running for the same
`user_id`. If another user's prompt races a shared-channel turn, it is
rejected with `error.code=cross_user_queue_unsupported` and should be
retried after the current turn finishes; this avoids running one user's
prompt through another user's memory/tool-credential context.

`request_credential` is fire-and-forget. It publishes an auth-prompt event and
returns immediately; prompt resolution happens through credential prompt
endpoints, OAuth callbacks, or the device-code poller. Chat messages are never
routed into an active credential prompt and never resolve/cancel that prompt.

**v1 scope is process-local.** The pending-prompt queue and auth-prompt
coordinator live in memory inside the API process. Docker worker/bot
services call back into the API as thin clients, so agent turns still execute
where those in-memory structures are authoritative. A future Redis-backed
backend can use the same queue/coordinator boundary if API agent execution is
ever scaled horizontally.

### Command Service

Most slash commands are handled by the central command service and return
markdown. Desktop, mobile, bots, and the agent `slash_command` tool use the
same registry. In-process callers execute through the direct backend adapter;
out-of-process surfaces call `POST /commands/execute`.

```http
GET /commands?actor=user&surface=desktop
```

`actor` may be `user`, `agent`, or `system`. `surface` may be `desktop`,
`mobile`, `cli`, `discord`, `telegram`, `slack`, `whatsapp`, `teams`,
`api`, or `agent`. The legacy
`source=user|agent|cli` query parameter still works; `actor` and `surface`
are preferred for new callers. Agent actor hides commands whose metadata marks
them unavailable to agents. Non-admin users do not see admin-only commands.

**Response:**

```json
[
  {
    "id": "tools.list",
    "path": ["tools", "list"],
    "name": "tools list",
    "description": "List tools: enabled (default), optional, core, or one category",
    "usage": "/tools list [enabled|optional|core|<category>]",
    "category": "Tools",
    "subcommands": [],
    "aliases": ["/tools_list", "/tools_enabled", "/tools_category", "/tools enabled", "/tools category", "/tools_core", "/tools core", "/tools_optional", "/tools optional"],
    "scope": "global",
    "surfaces": ["desktop", "mobile", "cli", "discord", "telegram", "slack", "whatsapp", "teams", "api", "agent"],
    "agent_allowed": true,
    "requires_thread": false,
    "requires_admin": false,
    "mutates_state": false,
    "danger_level": "safe",
    "execution_kind": "command",
    "note": null,
    "blocked_surfaces": [],
    "blocked_reason": null,
    "examples": [],
    "params": [
      {
        "name": "filter",
        "kind": "positional",
        "type": "str",
        "required": false,
        "choices": [],
        "choices_ref": null,
        "default": "enabled",
        "repeatable": false,
        "aliases": [],
        "description": "Which tools to list (default: enabled)",
        "no_echo": false,
        "label": "enabled|optional|core|<category>"
      }
    ]
  }
]
```

`params` is the declared argument schema (backlog #129). `null` means the
command has no schema (its handler parses free-form; 10 commands are exempt
by design: act-now commands, raw-text grammars, the two provider setup
chains, and `/help`). An array, even empty, means the dispatcher validates
arguments BEFORE the handler runs and renders uniform usage errors; `[]` is
the strict zero-argument declaration. Each entry:

| Field | Meaning |
| --- | --- |
| `name` | snake_case identifier; the bound value's key |
| `kind` | `positional`, `option` (`--name value` and `--name=value`), `flag` (boolean), `rest` (free text tail), or `scope` (trailing `global\|thread` token) |
| `type` | `str`, `int`, or `bool` |
| `required` | missing value is a usage error |
| `choices` | statically enforced value set (matched case-insensitively) |
| `choices_ref` | names a DYNAMIC value set (`models`, `providers`, `fallback_models`, `tools`, `skills`, `threads`, `triggers`, `hooks`, `mcp_servers`); advisory metadata, never enforced by the dispatcher. Every ref resolves live via `GET /commands/options/{ref}` (one server-side resolver registry, `core/command_option_resolvers.py`, serves that endpoint, generated picker forms, and Discord autocomplete) |
| `default` | applied when the param is absent |
| `repeatable` | collects a list (`--cond a --cond b`, or all bare tokens for a repeatable positional) |
| `aliases` | alternate spellings, e.g. `-y` for `--yes` |
| `description` | short human copy, feeds help cards and consumers |
| `no_echo` | validation errors never echo the rejected value (secret-adjacent params) |
| `label` | display override for usage strings and error copy (advertised form when it differs from the enforced one) |

For schema'd commands the `usage` string is GENERATED from `params` and
cannot drift from what the dispatcher enforces.

`surfaces` filters DISCOVERY only (menus, the `/help` index and `/help all`
table, this endpoint): `execute()` deliberately ignores it so bots can
forward surface-hidden subcommands like `/hook create`. The per-command help
card (`/help <cmd>`) filters its subcommand table by the ENFORCED axes
instead, so a chat surface sees every subcommand the bot passthrough can
actually execute there. `blocked_surfaces` is the enforced axis: execution
on a listed surface is refused with a markdown error that includes
`blocked_reason` and the surfaces the command is available on. As of
2026-08-02 it is set on `provider setup` and `provider cliproxy`, whose
typed flows would persist secrets in chat-platform message history.
`examples` carries ready-to-paste invocations for help renderers.

```http
POST /commands/execute
```

**Request:**

```json
{
  "command": "/tools list core",
  "thread_id": "abc123",
  "source": "user",
  "actor": "user",
  "surface": "desktop",
  "supports_forms": false
}
```

`supports_forms` (default false) is the form-capability flag: set it ONLY
when this caller renders `data.form` payloads (today: the Rich CLI). It
gates two behaviors. Without it, form payloads are STRIPPED from the
response at the dispatcher (the markdown fallback carries everything a form
does by construction; `data.state` hints and the `execution_kind` refusal
payload survive). With it, a schema'd command invoked WITHOUT its required
argument is rescued into a generated picker (below) instead of the usage
error; agents never rescue regardless of the flag, and extras, typos, and
invalid values stay errors for every caller.

**Response:**

```json
{
  "success": true,
  "markdown": "### Core Tools\n\n...",
  "command": "tools list",
  "level": "info",
  "data": null
}
```

**Result levels (2026-08-03).** Handlers author a typed outcome:
`level` is one of `info` (a readout: lists, status views), `success` (a
completed action), `warning` (completed with a caveat), or `error`.
`success` is `level != "error"`. The dispatcher is the single producer
of the markdown outcome artifacts, rendered FROM the level: `error`
bodies open with `**Error:** `, `success` with `**Done.** `, `warning`
with `**Warning:** `; `info` gets no artifact (plus the heading rule: a
multi-line info body whose first line is plain text has it promoted to
`### `). Clients may branch on `level` or on the artifacts; they share
provenance and cannot disagree. Two invariants: `data` is dropped when
`level` is `error` (a failure never ships a form or state hint) and
survives `warning`; and `info` is reported honestly (older backends
collapsed it into `success`, so a bare success level from them does not
imply a confirmation). The retired `[Error]:`/`[Success]:`/`[Info]:`
string prefixes never appear in first-party output anymore, including
the chat-stream command surfaces (`/goal`, `/orchestrate`, `/quick`,
`/done`, skill relays), which now speak the same artifact vocabulary
over SSE; the dispatch boundary still ACCEPTS the old prefixes from
out-of-tree handlers (plugins) as a permanent compatibility path.

Commands that require an active thread return a markdown error if `thread_id`
is omitted. `/compact` appears in discovery with `execution_kind:
"chat_stream"`, but `POST /commands/execute` returns a markdown error telling
the caller to route it through chat streaming instead; that refusal carries
`data: {"execution_kind": "chat_stream"}` so generic passthroughs (the
chat-platform bots) can detect it structurally and re-route the raw command
text into their normal chat path instead of string-matching the error copy.
The desktop and mobile composers derive their chat-stream routing set from
this discovery field too, so new `chat_stream` registrations need no client
change.

**User-defined aliases (2026-08-03, backlog #133).** The `/alias` family
(`create`, `delete`, `list`, bare root = the listing) maintains a per-user
alias table: a single-word spelling that expands to a whole command, values
included (`/gpt5` -> `/model openai/gpt-5.5`). Expansion happens
server-side inside `execute()` before parsing, so it works identically on
every surface that reaches the dispatcher (CLI, Telegram, the GUIs, the
agent) and every gate reads the RESOLVED command: an alias can never widen
access. An alias always loses to a registered command spelling, both at
creation (refused) and later (if the catalog claims the name, the alias
goes dormant and `/alias list` says so). Rows are stamped by the authoring
path and verified at dispatch; a row edited outside the `/alias` command
is skipped as inert and flagged in the listing. Discord cannot type alias
spellings (its slash registry only knows registered names), but managing
them there works. Expansion values are single unquoted words; quoted
phrases are refused at creation.

**Guidance behavior (2026-08-02).** Unknown commands and unknown subcommands
answer with a nearest-match suggestion ("Did you mean `/provider`?") derived
from the registry. Bare `/help` returns a compact per-category index of root
commands; `/help all` returns the full usage table; `/help <command>` (and
`/<command> help`, including group roots like `/tools help`) returns one
command's card: usage, subcommands visible on the calling surface, aliases,
access notes, and `examples`. Family roots given a bad or missing subcommand
render their usage error from the registered `usage` string plus the derived
subcommand list, ending with a pointer to `/help <command>`.

`data` is usually `null`, and a FAILING result never carries `data` (one
exception: the `execution_kind` refusal above). On success it may carry the
structured payloads of
the declarative form contract (schema owned by `core/command_forms.py`):
`data.form` is a versioned form definition (v1: title, tabs of
search/text/radio/checkbox fields, and a submit command template with
`{key}` placeholders; on confirm the client substitutes the ACTIVE tab's
selected values and dispatches the resulting slash command; a tab may
declare its own `submit` template, which wins over the form-level default
while that tab is active, so tabs can mean different actions). A `text`
field is a free-typed value fed from the client's composer; `secret: true`
asks the client to mask the display and keep the value out of input
history, and secrets are NEVER echoed back into form payloads (in-flight
secret state lives server-side, e.g. `core/provider_setup.py`). A tab may
carry `active: true`, asking the client to open the form on that tab:
chained multi-step commands use it as a step rail, re-sending the reached
steps as tabs with the next undecided one active so arrowing between tabs
is back/forward navigation. How a client binds that navigation is its own
business, but a client whose typed value comes from one shared input (the
Rich CLI's composer) must not let step navigation swallow text editing: there,
Left/Right cross steps only from the value's start/end boundary, Tab/Shift-Tab
always cross, and each step keeps its own draft so navigating the rail cannot
destroy a typed value. `footer_hint` is advisory copy for exactly this reason:
the server words it for the active step's field kind, and the client owns the
actual key map. Chained steps may carry `notes` on the form (the step's
delta lines: "Callback delivered", "Login failed: ..."); a form-rendering
client may print only the notes and let its panel carry the static rail
instead of reprinting the full markdown every step. Because printing notes
suppresses the rest of the markdown on those clients, the server attaches
them only to re-renders of a rail the surface has already printed, never to
a step whose guidance is load-bearing (a review table, a fresh auth URL, a
degraded-list caveat). Notes never carry information absent from the
markdown fallback, so form-less surfaces lose nothing.

A tab with NO fields is a described ACTION (#139, 2026-08-03): it carries
its own placeholder-free `submit` template, which the client dispatches
AS-IS on Enter, and an optional tab `description` rendered where a fielded
tab shows its input or list. The empty-selection no-op applies only to
templates that HAVE substituted `{key}` placeholders; a placeholder-free
template always dispatches. `/provider <name>`'s Set up and Test tabs are
the first producers.

`data.state` is a
dict of
client-state sync hints (for example `{"model": ...}` after a model change,
`{"reasoning": {"enabled": ..., "effort": ...}}` after `/think` or its
`/reasoning`/`/thinking` aliases change thinking mode, carrying the level the
model will actually run at, or `{"switch_thread": {"thread_id": ...}}` after
`/thread switch`). The
markdown fallback is always present, and form payloads ship only to callers
that sent `supports_forms` (the Rich CLI); `data.state` ships to everyone
and non-form frontends simply ignore it.

**Generated pickers (2026-08-03).** Beyond the hand-authored forms, ANY
schema'd command whose sole required positional has an option set (static
`choices`, or a `choices_ref` with a resolver) answers a bare invocation
from a form-capable caller with a picker generated from its declaration:
one tab (or one per WRITABLE scope for commands with a `scope` param:
distinct field keys per tab, the global tab only for admins), a filter
line past 10 options, and a submit template built from the declared path.
Resolver `current` markers are stripped for every ref except `threads` (a
shared resolver cannot know which command it feeds, and a wrong default
under the cursor is worse than none; re-switching to the current thread
is a no-op). Free-text arguments (thread titles, prompts) keep
the plain usage error by design, as do secret-adjacent grammars
(`no_echo`), multi-required shapes, and `fallback set` (an ordered chain a
single-select picker would misrepresent). Examples live today on
`/thread switch`, `/tools enable|disable`, `/skills enable|disable`,
`/triggers enable|disable|delete`, `/hook enable|disable|delete`,
`/mcp test|retry|remove`, `/fast set`, `/smart set`, `/provider switch`,
and `/fallback remove` (whose option set is the configured chain itself,
not the model catalog).

Hand-authored forms: bare
`/model` (model picker), bare `/provider` (Providers tab submitting into
the per-provider action step `/provider <name>`, plus a CLIProxy tab over
the subscription catalog behind `/provider cliproxy`; every caller gets the
picker, with the CLIProxy tab dropped for callers its submit target
refuses), `/provider <name>` itself (the action step: Use tab submitting
`/provider switch <name> global|thread`, plus admin-visible Set up /
CLIProxy / Test action tabs), the
`/provider setup` chain (masked key entry or Keep/Replace/Clear, API mode,
base URL, live model list fetched with the pending credentials, review,
then a test-first atomic apply in one settings patch), the `/provider
cliproxy` OAuth chain (target, login rail, model pick, apply), and bare
`/think` (thinking level per writable scope, a "This thread" tab only when
a thread is active). Clients that render forms should treat unknown
versions or field kinds as "render the markdown instead".

```http
GET /commands/options/{ref}?thread_id=abc123&q=gpt&limit=25
```

Resolves a `choices_ref` value set live, scoped to the calling identity
(threads, hooks, and triggers are the caller's own; `mcp_servers` answers
empty for non-admins, mirroring the `/mcp` gates). Returns a list of
`{id, label, meta, description, current}` options, the same shape form
option lists use, so a client can feed them straight into a picker or an
autocomplete. Optional `q` narrows server-side (casefolded substring over
id, label, and meta) and `limit` caps the list (0 = uncapped, max 100),
so latency-bound consumers such as autocomplete never ship a full
catalog. 404 for a ref no resolver claims; a resolver fault degrades to
an empty list, never an error. Discord autocomplete is this endpoint's
first remote consumer (acting as the invoking user's linked account); the
CLI palette and GUI argument stages (#135) are the intended next ones.

### Chat Slash Commands

The `/chat` endpoint keeps streaming slash commands that are not simple
request/response commands. Send the command as the message:

| Command | Description |
|---------|-------------|
| `/compact` | Manually trigger context compaction. Emits `compacting`, `compacted`, then a `response` confirmation when compaction starts; skipped compaction returns only a `response` and `done`. |

Example:
```json
{"message": "/compact", "thread_id": "abc123"}
```

Response:
```
data: {"type": "compacting", "message": "Compacting thread context...", "thread_id": "abc123"}
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

**Transcript rendering note:** the desktop and mobile GUI clients consume this stream for lifecycle and dashboard signals only (task start/end, thread-list activity, notifications, approvals, sync events). They render autonomous turn transcripts by attaching to the per-thread turn buffer (`GET /threads/{thread_id}/turn/stream`, see "Re-attach to a Turn") on the `task_started` signal. The Discord and Telegram bots do the same (attach-preferred, falling back to rendering this stream's transcript events when a turn is not attachable); the CLI still renders transcripts directly from this stream, so turn-output chunks keep flowing here (dual-feed).

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

**Fanout mirrors:** a prompt queued into a busy thread observes the holder
turn's output and republishes it on this stream under its own `task_id`. Such
mirrored events (transcript chunks and the queuer's `task_started`/
`task_completed`) carry `fanout: true`. Consumers that render turn output
must drop events with `fanout: true` wholesale; the holder's own delivery is
the single canonical copy. All built-in consumers (bots, CLI, GUI stores) do
this.

**Event Types:**

| Event | Description | Fields |
|-------|-------------|--------|
| `task_started` | Autonomous execution begins | `thread_id`, `task_id`, `prompt`, optional `todo_id`, `trigger_id`, `trigger_name`, `callable_name`, `source`, `handoff_id`, `caller_thread_id`, `caller_thread_name` |
| `thinking` | Agent reasoning | `content` |
| `tool_call_delta` | Status-only hint that the model is streaming tool-call argument chunks before the tool starts | none |
| `tool_call` | Tool invocation | `id`, `name`, `args`, `started_at` (server ISO-8601 UTC), `timeout_seconds`; on a managed MCP tool also `tool_type` (`mcp_server`), `server_id`, `server_name` |
| `tool_result` | Tool execution result | `id`, `name`, `result`, `started_at`, `duration_ms` (server-measured execution time); on a managed MCP tool also `tool_type`/`server_id`/`server_name` |
| `workspace_artifact` | Downloadable workspace file generated during the task | `tool_call_id`, `tool_name`, `path`, `name`, `mime_type`, `size_bytes` |
| `reply_suppressed` | The `react` tool requested reply suppression for this turn (registry-verified); the origin bot drops reply text | `tool_call_id` |
| `tool_reload` | Tool registry was reloaded mid-turn; resume metadata for next iteration | `tools`, `ttl`, `ttl_seconds`, `source`, `skill_name`, `reason` |
| `provider_retry` | Retryable provider/model failure; backend is sleeping before retry. If the provider failed after partial stream output, the backend rewound to the latest stable checkpoint first. | `provider`, `model`, `attempt`, `max_retries`, `delay_seconds`, `reason`, optional `http_status`, optional `rewound`, optional `stream_chunks` |
| `provider_fallback` | The backend switched to a configured fallback provider/model: primary retries were exhausted (may follow a post-stream rewind), or an empty provider refusal was swapped-and-re-run (`reason: refusal`, no retry loop). A consented switch may carry the user-chosen hold (`permanent: true` for an until-reverted hold). | `from_provider`, `from_model`, `to_provider`, `to_model`, `hold_seconds`, `expires_at`, optional `permanent`, `reason`, optional `http_status`, optional `rewound`, optional `stream_chunks` |
| `image_input_unsupported` | The model rejected image/PDF input; the backend stripped the attachment (leaving a placeholder note) and retried the turn once, and recorded the model as image/file-incapable for later turns | `reason`, `model` |
| `output_truncated` | The turn hit its output-token cap; `produced_output: false` means reasoning consumed the whole budget and the turn emitted no text or tool call | `reason`, `produced_output`, `output_tokens`, `model` |
| `response_refused` | The provider ended the response with a refusal; `produced_output: false` means it fired before any text or tool call. `swapped: true` = the refusal-swap is re-running on the next fallback model; `swapped: false` = a visible note was attached to the message (or the rewind removed the exchange) | `produced_output`, `output_tokens`, `model`, `swapped` |
| `turn_rewound` | A pre-output refusal was rewound server-side (only prompt-plus-refusal exchanges; tool activity gates the rewind and the notice arrives as a trailing `response` chunk instead); truncate locally from `to_message_id`, restore `prompt` to the composer (controlled clients), or deliver `content` as reply text (bots) | `reason`, `removed`, `to_message_id`, `prompt`, `model`, `content` |
| `response` | Response text chunks | `content` |
| `context_attached` | Previous context summary attached to this autonomous prompt | `summary` |
| `compacting` | Context summary generation has started after the compaction path passes its start checks | `message` |
| `compacted` | Context was compacted | `messages_removed`, `auto_resumed`, `summary`; a background proactive idle compaction also sets `proactive: true` |
| `iteration_limit` | Agent hit a turn safety stop | `content`, `reason`, `scope`, `max_iterations`, `tool_call_count`, `resumable`, optional repeated-tool fields |
| `turn_resumed` | A `/resume` re-drive of a halted turn started | `tool_call_offset` |
| `notification` | Explicit `notify` tool event or new in-app notification | `message`, `summary`, `in_app_only` |
| `reaction_request` | The `react` tool asked the origin chat platform's bot to post an emoji reaction; the matching bot (by `platform`) performs it best-effort | `platform`, `channel_id`, `message_id`, `emoji` |
| `task_completed` | Execution finished | `notify`, `content`, `summary`, `todo_id`, optional `handoff_id`, `caller_thread_id`, `caller_thread_name` |

The same stream also carries cross-client sync events used by open frontends:

| Event | Description | Fields |
|-------|-------------|--------|
| `message_added` | Another client posted an interactive user message | `role`, `content` |
| `thread_created` | A thread/callable thread was created | `title`, `title_source`, `platform` |
| `thread_updated` | Thread metadata changed | `title`, `title_source`, `pinned`, `platform` |
| `thread_deleted` | A thread was deleted | none |
| `thread_rewound` | Trailing exchanges were removed via the rewind endpoint | `steps`, `removed`, optional `to_message_id` |
| `queue_restored` | A user-initiated stop returned queued user prompts unprocessed; other open clients should restore their local queued copies to the composer. Suppressed for the originating client via `X-Nymeria-Client-Id`. | `count`, `prompts` (list of raw prompt texts) |
| `thread_teams_changed` | Callable-team entities or membership changed (teams REST, config PATCH, `team_manage`, `nym.threads.configure` team=, a teamed spawn). The payload is a hint; clients refetch `GET /thread-teams`. | optional `team_id`, `reason` (`created`/`renamed`/`described`/`membership`/`updated`/`deleted`) |

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

Per-user isolation is driven by account tokens, not by trusted request bodies:

- Each account has its own profile, memories, threads, tools, skills, TODOs,
  activity feed, and notifications.
- Client-supplied `user_id` fields or query parameters are ignored on routes
  that use the authenticated-user dependency.
- Admin callers target another user with `X-Nymeria-Act-As: <user_id>`.
- The bootstrap owner account is usually `default` on single-user deployments.

**Example:**
```http
POST /chat
Authorization: Bearer nym_alice...

{"message": "My name is Alex"}
```

This stores any learned profile facts under the account that owns
`nym_alice...`, isolated from other users.

---

## Example: Python Client

### Synchronous Request

```python
import httpx

TOKEN = "nym_..."

response = httpx.post(
    "http://localhost:8000/chat/sync",
    json={
        "message": "Hello",
        "user_id": "my_user"
    },
    headers={"Authorization": f"Bearer {TOKEN}"}
)
print(response.json()["response"])
```

### Streaming Request

```python
import httpx

TOKEN = "nym_..."

with httpx.stream(
    "POST",
    "http://localhost:8000/chat",
    json={"message": "Search for Python tutorials"},
    headers={"Authorization": f"Bearer {TOKEN}"}
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

TOKEN = "nym_..."

async def chat():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/chat/sync",
            json={"message": "Hello"},
            headers={"Authorization": f"Bearer {TOKEN}"}
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

**Response:** includes LLM settings such as `llm_provider`, `llm_model`, `llm_base_url`, the model tier settings `llm_fast_model`, `llm_smart_model`, `llm_background_model`, and `llm_background_base_url` (each with a read-only `*_resolved` companion giving the effective `provider:model`), `llm_context_length`, `llm_ollama_num_ctx`, `llm_provider_route`, `openai_api_mode`, LLM stream retry settings, `llm_fallback_hold_seconds`, and the fallback-consent settings `llm_fallback_switch_mode`, `llm_fallback_prompt_timeout_seconds`, and `llm_refusal_swap_mode`; context settings such as `context_management`, `compact_threshold`, and `compact_keep_messages`; tool runtime settings such as `tool_output_max_chars`; plus voice runtime settings such as `tts_provider`, `tts_base_url`, `tts_model`, `tts_voice`, `tts_output_format`, `tts_speed`, `stt_provider`, `stt_base_url`, `stt_model`, `stt_language`, and `voice_default_thread_id`; plus RAG engine settings such as `embedding_provider`, `embedding_model`, `embedding_dimensions`, `rag_retrieval_mode`, `rag_rerank_enabled`, `rag_rerank_provider`, `rag_rerank_model`, and `rag_embed_tool_results`.

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
  "provider_route": "openai_compat",
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
API key env vars, aliases, Chat Completions support, Responses support, tier
metadata, route metadata (`supported_routes`, `default_route`,
`openai_compat_base_url`, and `anthropic_native_for_claude` for gateways that
expose the `anthropic_messages` route for Claude), and
`reasoning_passback_verified` (a durable flag marking that the provider's
reasoning-passback round-trip has been live-smoke-tested; the active mechanism
for a given thread is computed at runtime, see
`agent-systems/reasoning-streaming.md`). Desktop/mobile use this metadata for provider setup
and diagnostics; the authoritative implementation lives in
`nymeria/config/llm_providers.py`.

---

### Available Provider Models

```http
GET /models/available?provider=groq&base_url=https://api.groq.com/openai/v1
Authorization: Bearer <token>
```

Fetches models from the selected provider's live `/models` endpoint. `provider`
defaults to the server's `LLM_PROVIDER`; `base_url` is optional and lets the UI
test unsaved OpenAI-compatible endpoint overrides. The endpoint resolves auth
from the credential vault first, then settings/env vars.

**Stored credentials do not follow a caller-supplied `base_url`.** When a
non-admin passes `base_url`, the vault and settings key resolution is skipped
for that request, so the named endpoint receives an unauthenticated probe. A
keyless endpoint (Ollama, LM Studio, a local gateway) still lists normally; a
provider that requires a key returns `[]` rather than sending the server's key
to an address chosen by the request. Admins are exempt, since they already
reach the same combination through the POST variant below. This is why
listing models against an unsaved *authenticated* endpoint belongs on POST.

Returned model objects
include `id`, `name`, `owned_by`, `created`, and any normalized metadata the
provider exposes, such as `context_length`, `max_completion_tokens`,
`supported_parameters`, and `input_modalities`.

Chat Completions responses standardize usage token fields, but model context
window is not standardized. When `/models/available` sees context metadata,
Nymeria caches it so `/threads/{thread_id}/context` can report frontend context
percentage more accurately.

```http
POST /models/available
Content-Type: application/json
Authorization: Bearer <admin-token>

{"provider": "groq", "base_url": "https://api.groq.com/openai/v1", "api_key": "..."}
```

The POST variant (admin-only, like the provider test endpoint) accepts an
EPHEMERAL `api_key` override in the request body: the key is used for that
one listing and never stored, logged, or echoed. POST keeps the key out of
URLs and access logs; it backs the `/provider setup` flow's model step,
where a just-pasted key lists models before anything is saved (a successful
list doubles as a credential probe). All fields are optional: an omitted
`provider` or `base_url` resolves like the GET's query params (active
settings), and an omitted `api_key` falls back to the stored credential for
the effective provider.

Both routes, and the in-process command facade that slash commands use, run
the same single implementation: the module-scope `_available_models` in
`nymeria/api/routers/settings.py`. Keep it that way. The in-process path once
had a hand-copied twin, and the copy drifted (it was missing the CLIProxy
`/v1` normalization and the probe headers), so the same command returned
different model lists depending on which runtime shape served it.

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
  "llm_fast_model": "anthropic/claude-haiku-4.5",
  "llm_smart_model": "anthropic/claude-opus-4",
  "llm_background_model": "openai:gpt-4o-mini",
  "llm_background_base_url": "",
  "llm_fallback_models": ["anthropic:claude-haiku-4-5-20251001"],
  "llm_temperature": 0.7,
  "llm_max_tokens": 4096,
  "llm_top_p": 0.95,
  "llm_top_k": 40,
  "llm_frequency_penalty": 0.5,
  "llm_presence_penalty": 0.3,
  "llm_reasoning_effort": "medium",
  "llm_use_model_defaults": false,
  "llm_context_length": 128000,
  "llm_ollama_num_ctx": 32768,
  "llm_provider_route": "native",
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
  "llm_fallback_hold_seconds": 7200,
  "tool_output_max_chars": 100000
}
```

| Field | Type | Range | Description |
|-------|------|-------|-------------|
| `llm_provider` | string | - | Provider ID. `anthropic` uses Anthropic Messages; OpenAI-compatible IDs are listed by `GET /settings/llm/providers`. |
| `llm_model` | string | - | Model identifier |
| `llm_fast_model` | string | - | Fast model tier for the `/fast` command and the `fast` alias. A `model-id` or `provider:model-id`. Unset uses a provider-aware default. |
| `llm_smart_model` | string | - | Smart model tier for the `/smart` command and the `smart` alias. A `model-id` or `provider:model-id`. Unset falls back to `llm_model`. |
| `llm_background_model` | string | - | Background/utility model tier for the `/background` command and the `background` alias; powers the `extraction_prompt` step (`fetch_url_nymeria`, `file_read`). A `model-id` or `provider:model-id`. Unset falls back to `llm_model`. |
| `llm_background_base_url` | string | - | Optional base URL override for the background tier (e.g. a local model server or CLIProxy). Blank inherits the resolved provider's base URL like the fast/smart tiers. |
| `llm_fallback_models` | string[] / comma string on PATCH | - | Ordered backend model fallback chain used after primary retries are exhausted. PATCH accepts a comma-separated string; entries may be `model-id` or `provider:model-id`. |
| `llm_temperature` | float | 0.0-2.0 | Sampling temperature |
| `llm_max_tokens` | int | 1-1000000 | Max output tokens |
| `llm_top_p` | float | 0.0-1.0 | Nucleus sampling threshold |
| `llm_top_k` | int | 1-100 | Top-k sampling |
| `llm_frequency_penalty` | float | -2.0-2.0 | Reduce repetition |
| `llm_presence_penalty` | float | -2.0-2.0 | Encourage new topics |
| `llm_reasoning_effort` | string | off/low/medium/high/xhigh/max | Reasoning effort for compatible models. `off` explicitly disables thinking; unsupported levels are adjusted onto the model's supported range at request time (over-asks drop to its ceiling) |
| `llm_use_model_defaults` | bool | true/false | Use model-specific defaults for temperature/top_p/frequency_penalty |
| `llm_context_length` | int | 1000-2000000 | Manual context-window override for local endpoints or proxies that do not report context metadata |
| `llm_ollama_num_ctx` | int | 1000-2000000 | Ollama runtime context override sent as `extra_body.options.num_ctx` on Chat Completions requests |
| `llm_provider_route` | string | `native`/`openai_compat`/`anthropic_messages` | Default adapter route for providers that support more than one route. `google` and `ollama` default to `native`; the Claude-serving gateways (`litellm`, `opencode`, `zenmux`, `requesty`, `fastrouter`, `poe`) offer `anthropic_messages` and default to `openai_compat`; per-thread settings can override this. |
| `openai_api_mode` | string | `responses`/`chat_completions` | Default OpenAI-compatible API mode. `responses` is honored only for registry providers that advertise Responses support; Chat Completions is the compatibility baseline. |
| `openai_api_key` | string | - | Write-only OpenAI or OpenAI-compatible global API key |
| `anthropic_api_key` | string | - | Write-only Anthropic global key. For Anthropic CLIProxy this is the local `cpx-*` gatekeeper key. |
| `anthropic_direct_api_key` | string | - | Write-only direct Anthropic key used when the effective Anthropic base URL is empty |
| `openrouter_api_key` | string | - | Write-only OpenRouter global API key |
| `embedding_api_key` | string | - | Write-only OpenAI-compatible embeddings key. Do not set this to a CLIProxy `cpx-*` gatekeeper key. |
| `gemini_api_key` | string | - | Write-only Gemini key for Gemini-backed capabilities |
| `perplexity_api_key` | string | - | Write-only Perplexity key for web search |
| `llm_stream_max_retries` | int | 0-10 | Retries for transient LLM call/stream failures. Post-stream failures rewind to the latest checkpoint before retrying. |
| `llm_stream_retry_initial_delay` | float | 0-60 | Initial LLM retry backoff delay in seconds |
| `llm_stream_retry_max_delay` | float | 0-300 | Maximum LLM retry backoff delay in seconds |
| `llm_fallback_hold_seconds` | int | 0-604800 | Seconds to keep a fallback provider/model active for the thread after retries are exhausted. Default is 7200. |
| `llm_fallback_switch_mode` | string | `auto`/`ask` | How a transport fallback (primary exhausted retries) is applied. `auto` (default) swaps silently; `ask` parks a consent-capable interactive turn on a `fallback_prompt` (timeout auto-swaps). Per-thread overridable via `llm_config.fallback_switch_mode`. |
| `llm_fallback_prompt_timeout_seconds` | int | 10-600 | How long an `ask`-mode consent prompt waits before auto-swapping. Default is 180. |
| `llm_refusal_swap_mode` | string | `off`/`ask`/`auto` | Whether an EMPTY provider refusal discards the refused response and re-runs the call on the next fallback model. Default `ask` (parks a consent-capable interactive turn; other turns auto-swap); `auto` swaps silently everywhere; `off` keeps the rewind-and-restore recovery only. Per-thread overridable via `llm_config.refusal_swap_mode`. |
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

Native optional tools prefer saved credentials from the user/system credential vault. The same settings endpoint can still manage deployment-wide fallback env fields for integrations, including `DHL_API_KEY`, `ONFLEET_API_KEY`, `PHANTOMBUSTER_API_KEY`, `WEBFLOW_ACCESS_TOKEN`, `LEMLIST_API_KEY`, `SENDY_API_KEY`, `EMELIA_API_KEY`, `AFFINITY_API_KEY`, `KEAP_ACCESS_TOKEN`, `MAGENTO_ACCESS_TOKEN`, `UNLEASHED_API_KEY`, `DRIFT_ACCESS_TOKEN`, `OKTA_ACCESS_TOKEN`, `MAUTIC_BASE_URL`, `RUNDECK_BASE_URL`, `RUNDECK_TOKEN`, `KOBOTOOLBOX_API_TOKEN`, `KOBOTOOLBOX_BASE_URL`, `QUICKBOOKS_ACCESS_TOKEN`, `QUICKBOOKS_REALM_ID`, `XERO_ACCESS_TOKEN`, `XERO_TENANT_ID`, `MICROSOFT_GRAPH_ACCESS_TOKEN`, and `MICROSOFT_GRAPH_BASE_URL`; see `tools.md` for the exact provider and field names.

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
| `supported_reasoning_efforts` | string[] | Reasoning-effort levels this model accepts (subset of off/low/medium/high/xhigh/max); requests outside the set are adjusted server-side (over-asks drop to the ceiling) |
| `max_reasoning_effort` | string | Highest supported reasoning-effort level; frontends warn when the configured effort exceeds it |
| `input_modalities` | string[] | Supported input types: `text`, `image`, `file` |
| `tokenizer` | string \| null | Tokenizer family: `Claude`, `GPT`, `Llama3`, etc. |
| `default_temperature` | float \| null | Model's default temperature (shown when "Use model defaults" is enabled) |
| `default_top_p` | float \| null | Model's default top_p |
| `default_frequency_penalty` | float \| null | Model's default frequency penalty |

---

### List Threads

```http
GET /threads?owned_only=false
Authorization: Bearer <token>
```

Returns all threads with server-authoritative metadata (titles, pins, platform).
The effective user comes from the bearer token or admin `X-Nymeria-Act-As`;
client-supplied `?user_id=` values are ignored. Set `owned_only=true` for
cleanup tooling and tests that should skip recovered checkpoint/resource rows.
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
| `platform` | Origin surface: `"desktop"`, `"callable"`, `"discord"`, `"telegram"`, `"slack"`, `"whatsapp"`, `"teams"`, `"trigger"`, `"webhook"` |
| `callable` | Whether the saved per-thread config currently marks the thread callable |
| `recovered` | `true` when this row was included because a resource survived without the normal complete thread listing path and the effective caller can open it |
| `recovery_sources` | Storage surfaces that referenced the recovered thread, e.g. `"metadata"`, `"todo"`, `"scheduled_todo"`, `"trigger"`, `"chat_binding"`, `"bind_code"`, `"checkpoint"` |

---

### Update Thread Metadata

```http
PATCH /threads/{thread_id}/metadata
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

Updates server-side display metadata. Callable thread tool names are edited
through `PATCH /threads/{thread_id}/config` with `callable_name`; the thread
list derives a callable thread's visible title from `callable_name`.

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

### Trigger Thread Dream

```http
POST /threads/{thread_id}/dream
Content-Type: application/json
Authorization: Bearer <token>
```

Starts a manual dream cycle for an owned thread. The API creates a temporary
shadow thread with the dream system prompt, a strict memory/TODO/instructions
tool policy, and `shadow_parent_id` pointing at the parent. The dream turn runs
in the background and streams through the autonomous event feed under the
returned `shadow_thread_id`.

The parent thread must have `dreaming.enabled=true` in its thread config unless
`force=true` is supplied. The endpoint returns `409` if the parent is actively
processing and `400` if called on a shadow thread.

**Request Body:** (optional)
```json
{
  "model": "claude-haiku-4-5-20251001",
  "force": false
}
```

**Response:**
```json
{
  "shadow_thread_id": "dream-parent-20260524T120000-a1b2c3",
  "parent_thread_id": "parent",
  "started_at": "2026-05-24T12:00:00+00:00",
  "model": "claude-haiku-4-5-20251001",
  "enabled_optional_tools": ["thread_instructions_set"],
  "disabled_core_tools": ["bash_execute", "file_read"]
}
```

---

### Stop Thread

```http
POST /threads/{thread_id}/stop
Authorization: Bearer <token>
```

Aborts a running stream on the thread. Cascades to any active callable child
threads. Any prompts still waiting on the sub-turn queue are handled by
source: queued user prompts are returned to the caller unprocessed
(restore-to-composer semantics, never auto-resent), while programmatic
queuers (callable threads, MCP, triggers, ticker) are woken with an abandon
signal as before. Each restored queuer's own stream is terminated with an
`error` frame carrying `code: "restored"`.

**Response:**
```json
{
  "status": "stopping",
  "thread_id": "abc123",
  "message": "Stop signal sent. Thread was held by 'chat' for 12s. Will stop at next iteration boundary.",
  "holder": "chat",
  "held_seconds": 12.4,
  "restored_prompts": [
    {
      "text": "the queued message text",
      "source_label": "User",
      "user_id": "default",
      "enqueued_at": 1767052800.0
    }
  ]
}
```

`holder` and `held_seconds` are the structured forms of the values embedded
in `message`. `restored_prompts` lists the queued user prompts handed back
by this stop, oldest first (empty when nothing was queued). Clients should
place the `text` values back into the composer for the user to edit or
resend. When the stop restores at least one prompt, the autonomous stream
also carries a `queue_restored` sync event (origin-suppressed via
`X-Nymeria-Client-Id`) so other open clients can restore their local queued
copies.

If the thread is idle, the response is `{"status":"idle","thread_id":"abc123",
"message":"Thread was not running. No stop signal needed.",
"restored_prompts":[]}`.

---

### Claim Thread Ownership

```http
POST /threads/{thread_id}/claim
Authorization: Bearer <token>
```

Eagerly registers the calling user as the owner of `thread_id` in the `thread_owners` table. The desktop frontend calls this from `threadsStore.createThread()` immediately after generating a UUID, and the CLI calls it for CLI-generated startup threads and `/thread create`. This gives the backend an ownership row before any chat-app routing (Telegram/Discord via `X-Nymeria-Act-As`) can hit `/chat` and TOFU-claim the thread for someone else.

Idempotent  -  safe to call multiple times. The request body is optional. First-party clients may seed metadata:

```json
{
  "title": "CLI Draft",
  "platform": "cli"
}
```

**Response (200):**
```json
{
  "thread_id": "abc123",
  "owner": "default"
}
```

When metadata is supplied, the response includes the stored metadata fields:

```json
{
  "thread_id": "abc123",
  "owner": "default",
  "title": "CLI Draft",
  "platform": "cli"
}
```

**Errors:**
- `400`  -  `thread_id` matches a shared-channel pattern (`discord_<g>_<c>`, `telegram_-<id>`, `slack_C...`, `whatsapp_group_<id>`). These are inherently multi-user and cannot be per-user-claimed.
- `404`  -  Non-admin caller and the thread is owned by someone else. Mirrors `_require_thread_access`'s leak surface so callers can't probe ownership under other users. Admin callers always get `200` with the actual owner instead.

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

Returns all user IDs whose TODO file currently contains at least one TODO item. Empty legacy or stale TODO files are ignored so thin-client pollers do not poll no-op lists or try to impersonate deleted platform-only users.

No query parameters.

**Response:**
```json
["default", "discord_123456789012345678", "telegram_123456789"]
```

---

### Thread TODO Counts

```http
GET /todos/thread-counts
Authorization: Bearer <token>
```

Returns active TODO counts keyed by thread ID for the effective user. The
frontend uses this for sidebar badges.

**Response:**
```json
{
  "thread-xyz": 2,
  "default-default": 1
}
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
      "recurrence": "1d",
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
  "recurrence": "2h",
  "thread_id": "optional-thread-id",
  "notes": "Optional notes"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task` | string | Yes | Task description |
| `scheduled_for` | string | No | Relative (`45s`, `17m`, `2h`, `1w`) or absolute/ISO datetime |
| `recurrence` | string | No | Interval as a canonical duration string (`Nm`, `Nh`, `Nd`, `Nw`; or `Ns` with a 60s minimum). Examples: `"5m"`, `"2h"`, `"1d"`. Legacy preset names (`hourly`, `daily`, `weekly`, `monthly`, `5min` … `30min`) are accepted on input and normalised to canonical form on storage. |
| `thread_id` | string | No | Thread for autonomous output, defaults to a user-scoped default thread |
| `notes` | string | No | Additional context |
| `workflow_id` | string | No | Create-only: run this published workflow tool headlessly at the scheduled time instead of an agent turn. The binding is validated at create time (400 on a missing tool, unapproved revision, or uncovered required parameters). The run never delivers output by itself; the workflow must deliver explicitly. To change a binding, delete and recreate the TODO. |
| `workflow_params` | object | No | Parameters bound to the scheduled workflow run |

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
  "recurrence": "1w",
  "clear_recurrence": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `task` | string | Updated task text |
| `status` | string | `pending`, `in_progress`, `done` |
| `notes` | string | Set or update notes |
| `scheduled_for` | string | Set or update next scheduled execution |
| `recurrence` | string | Interval as a canonical duration string (e.g. `"5m"`, `"2h"`, `"1d"`, `"1w"`); minimum 60s. Legacy preset names accepted as on create. |
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
GET /activity?limit=50
Authorization: Bearer <token>
```

Returns recent activity entries (autonomous tasks, tool executions, etc.) for
the effective user.

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

Nymeria's notification system has two surfaces: the **in-app feed** (audit
log of every notify call) and **destinations / profiles / preferences**
(user-configured routing). See [`notifications.md`](notifications.md) for
the full reference.

### In-app feed

```http
GET    /notifications
POST   /notifications/{notification_id}/read
POST   /notifications/read-all
DELETE /notifications/{notification_id}
DELETE /notifications
```

`GET /notifications` response (rows include audit-log metadata):
```json
{
  "notifications": [
    {
      "id": "notif123",
      "summary": "Found important email",
      "thread_id": "thread-xyz",
      "task_id": "todo-abc",
      "created_at": "2026-02-02T10:00:00Z",
      "read": false,
      "profile": "default",
      "attempted": ["my-phone", "work-email"],
      "delivered_to": ["my-phone"],
      "errors": {"work-email": "No authenticated email account"}
    }
  ],
  "unread_count": 1
}
```

### Channel types, destinations, profiles, preferences

```http
GET    /notifications/channel-types

GET    /notifications/destinations
POST   /notifications/destinations
GET    /notifications/destinations/{dest_id}
PATCH  /notifications/destinations/{dest_id}
DELETE /notifications/destinations/{dest_id}
POST   /notifications/destinations/{dest_id}/test

GET    /notifications/profiles
POST   /notifications/profiles
GET    /notifications/profiles/{profile_id}
PATCH  /notifications/profiles/{profile_id}
DELETE /notifications/profiles/{profile_id}

GET    /notifications/preferences
PATCH  /notifications/preferences
```

### Send an external notification

```http
POST /notifications/external
Authorization: Bearer <token>
Content-Type: application/json

{"message": "2 TODO(s) stale on thread th-1", "thread_id": "th-1"}
```

Delivers `message` to the external destinations in the caller's `default`
notification profile (no in-app feed row is written). Returns
`{"delivered_to": ["<destination-name>", ...]}`.

This is the vault-safe transport for thin services that hold only the
service token (never the master secrets key): they call this endpoint with
`X-Nymeria-Act-As: <user_id>` and the API performs the channel dispatch.
(The watchdog sweep, its original caller, now dispatches in-process from
the ticker; the endpoint remains the generic thin-client surface.)
Non-admin account tokens are pinned to their own user by Act-As
resolution, so a regular user can only notify themselves.

The thread config endpoint also exposes a per-thread override:
`PATCH /threads/{thread_id}/config` accepts `notification_profile` (string)
and `clear_notification_profile` (bool).

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
    "idle_timeout_seconds": 300,
    "call_timeout_seconds": 60
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

This route is a live invocation, not a dry run: it reaches the executors
directly off the stored definition. It therefore enforces the same
execution-trust gate the agent-facing tools do, for all four implementation
types, and returns **409** with an `approval_required` detail for a definition
that no authoring path approved (typically one written straight into
`data/custom_tools/`). Re-save it through `tool_create` or the tools admin UI to
approve the current revision. See `agent-systems/tool-hot-loading.md`.

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

## Workflow Approvals API

Custom tools with `implementation_type: "workflow"` carry a per-revision
approval gate: a content hash over `{source, entrypoint, continuations,
parameters}` must be admin-approved before the workflow can run. These routes
are the resolution surface. Approval is deliberately REST-only (no agent tool
can approve), so a prompt-injected turn cannot satisfy the gate.

### List Pending Workflow Revisions

**Admin-only.**

```http
GET /workflows/pending
Authorization: Bearer <admin-token>
```

Returns drafts and published workflow tools whose current revision is neither
approved nor declined. Each entry includes `kind` (`draft` or `tool`), `id`,
`owner_user_id`, `tool_id`, `author`, `revision` (the content hash), and
`parameter_names`.

### Review Workflow Source

**Admin-only.**

```http
GET /workflows/source?kind=draft&id=<id>&owner_user_id=<user>
Authorization: Bearer <admin-token>
```

The review view: the pending entry fields plus `source_code` and the current
`approval` state. `owner_user_id` is required for `kind=draft`.

### Approve / Decline a Revision

**Admin-only.**

```http
POST /workflows/approve
POST /workflows/decline
Content-Type: application/json
Authorization: Bearer <admin-token>
```

**Request Body:**
```json
{
  "kind": "draft",
  "id": "wf_daily_digest",
  "owner_user_id": "user-123",
  "note": "optional note for the author",
  "revision": "optional content hash the admin reviewed"
}
```

Approving pins the hash recomputed from content; declining records the
decision (and revokes a same-hash approval). The author is notified either
way. When `revision` is supplied and the content on disk no longer hashes to
it, the call fails with **409** so an admin never blesses an edit they did
not review. Unknown targets return **404**.

### Workflow Run Records

```http
GET /workflows/runs?limit=20
GET /workflows/runs/{workflow_id}?limit=20
Authorization: Bearer <token>
```

Recent run records (`run_id`, `status`, `timestamp`, envelope and step trace).
The bare `/runs` form aggregates across all workflows, newest first (the
dashboard feed shape); the per-workflow form scopes to one workflow. Admins
see every run; other callers see only their own. A refused resume
(`resume_invalid`) also leaves a stepless run record under the ack's
`run_id`.

### Runtime Approvals (nym.approve suspensions)

Distinct from revision approvals above: a running workflow that calls
`nym.approve(prompt, state, resume="continuation")` suspends with a durable
pending record and a `needs_approval` envelope. The run holds no live
process; resolution spawns a fresh subprocess into the declared continuation
with the persisted `state` and a `decision` dict. Resolution is also
REST-only and owner-or-admin (the run owner decides; a prompt-injected turn
cannot).

```http
GET /workflows/approvals
Authorization: Bearer <token>
```

Pending suspensions, newest first; non-admins see only their own. Entries
carry `record_id`, `workflow_id`, `origin`, `user_id`, `thread_id`, `prompt`,
`resume_entrypoint`, `created_at`, `expires_at` (never the resume token or
the raw state).

```http
POST /workflows/approvals/{record_id}/resolve
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{ "approved": true, "note": "optional note passed to the continuation" }
```

Claims the record atomically (a concurrent resolve gets **409**) and runs the
continuation in the background; the ack carries `run_id` so the outcome can be
read from `GET /workflows/runs/{workflow_id}`. A **declined** resolution ALSO
runs the continuation, with `decision.approved = false`, so the author can
clean up or notify. Missing and not-yours both return **404** (no existence
leak). Resume is refused (`resume_invalid` envelope, nothing executes) when
the workflow's content hash changed since the suspension, its revision
approval was revoked, or the continuation is no longer declared. Pending
records expire after 7 days: expiry resolves as declined with note
`expired`, driven by an hourly sweep in the API process.

SSE event types: `workflow_approval` (a run suspended; carries `record_id`,
`workflow_id`, `prompt`, `expires_at`) and `workflow_approval_resolved`
(carries `record_id`, `approved`, `note`, `run_id`). The owner also gets
normal notifications for both. Live run progress rides `workflow_step` (one
per completed verb dispatch; best-effort fire-and-forget, so sort by `step`)
and `workflow_run_finished` (every run's terminal status). Engine event
types are reserved: `nym.emit` refuses to publish them.

### Execute a Workflow

```http
POST /workflows/{workflow_id}/execute
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:**
```json
{ "params": {"name": "value"}, "thread_id": "optional-thread" }
```

Runs one published workflow tool as the caller and blocks until the run
finishes (bounded by the workflow's wall-clock cap); returns `{workflow_id,
run_id, envelope}`. The headless execution surface: the Docker worker relays
scheduled workflow TODOs here, and frontends can run a workflow directly.
The revision approval gate is re-checked at call time. Refusals map to
**404** (no such workflow tool) or **403** (unapproved revision, depth cap).
The run never delivers output anywhere by itself; delivery is the workflow's
own explicit job (`nym.thread` / `nym.notify`).

### Workflow Templates

```http
GET  /workflows/templates
POST /workflows/templates/{template_id}/install
Authorization: Bearer <token>
```

`GET` lists the bundled workflow-recipe catalog (curated JSONs shipped in
`nymeria/workflows_bundled/`, each statically validated at load, so a broken
bundled file is skipped rather than listed): `{"templates": [{id, name,
description, notes, parameters}], "total"}`, where `parameters` are the names
derived from the recipe's entrypoint signature.

`POST` installs one as a published workflow tool. **Admin-only**, unlike the
per-user hook-template install: a workflow tool is GLOBAL and its execution is
gated on an admin-approved revision, so installing one publishes and
self-approves a trusted revision. The (currently empty) JSON body is reserved
for forward-compatible options. The response is `{"created", "already_installed",
"template_id", "approval", "tool"}`. Install is idempotent by tool id:
re-installing returns the existing tool with `created=false` and overwrites
nothing; an id occupied by a *different* tool is a **400** conflict rather
than an overwrite, as is an unknown template id. The installed tool records
its origin in `tags` (`bundled`, `template:<id>`) and is otherwise an ordinary
workflow tool. This route publishes only; it does not enable the tool on any
thread (the agent-facing `tool_create(action="install_template")` does both).

---

## Callable Threads API

Callable threads replace the old sub-agent system. Any thread marked `callable=True` becomes a directly invocable tool  -  but only within threads owned by the **same user** that owns the callable. The tool registry is global, but `_build_graph_with_prompt` filters callables by ownership when building each user's graph, and the runtime gate in `agents/tool_factory.py` rejects cross-user invocations even on cache stale paths. Admins can route through another user's callables via `X-Nymeria-Act-As`.

Callable teams partition that owner-wide list into isolated bubbles (both directions since 2026-07-23, backlog #97). A caller thread receives only callable tools whose thread configs carry the SAME `callable_team_id`, where "no team" is itself a bubble: a teamed thread sees only same-team callables, and an unteamed thread sees only unteamed callables. A blocked invocation returns a clear error naming the fix (put both threads in the same team, or both outside teams). Threads spawned via `spawn_thread` (fresh and branched modes, including kit template-thread materialization) inherit the spawning thread's team, so a teamed thread can always invoke what it spawns. One deliberate exemption: the `nym.thread` workflow verb keeps owner-wide reach (workflows are admin-approved per revision and serve as the sanctioned cross-team orchestration surface).

### List Agent Templates

```http
GET /agents/templates
Authorization: Bearer <token>
```

Legacy template endpoint. It currently returns `{"templates": [], "total": 0}`.

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

**Request Body:**
```json
{
  "callable_name": "Researcher",
  "callable_description": "Research support",
  "system_prompt": "You are a focused research assistant.",
  "llm_provider": "openrouter",
  "llm_model": "anthropic/claude-sonnet-4",
  "llm_temperature": 0.2,
  "llm_max_tokens": 4096
}
```

---

### Callable Teams

Teams are entities in a per-user store (`data/teams/<user>.json`, managed by
`core/team_manager.py`) holding identity (name, description); membership stays
on each thread's config as `callable_team_id`. Names resolve from the store,
so a rename is an O(1) store write that touches no member thread config.

Beyond this REST surface, teams are agent-manageable (backlog #100 phase 2):
the `team_manage` catalog tool (full CRUD on the acting user's own teams,
team refs by id or name, thread refs by id or callable name), `team=` on the
`spawn_thread` tool ("none" opts a child out of the inherited team) and on
the `nym.threads.configure` workflow verb, and the read-only `/team list` and
`/team show` slash commands. Every mutation surface shares the same service
functions in `core/team_manager.py` and publishes the `thread_teams_changed`
sync event (see the sync-events table) so open clients refetch the team list.

Teams also carry a shared key-value memory (backlog #100 phase 3), stored on
the team entity and read/written by the team's threads through the standard
memory tools with `scope="team"` (`memory_add`/`memory_edit`/`memory_read`;
the full read renders the team's identity header: name, description, and the
teammate roster). Teamed threads get a third `memory_read(scope="team")` in
their session-start memory seed and in every post-compaction reseed; caps
ride the global key-value memory limits, applied per team. Entries index into
the owner's RAG store as `team_memory` chunks, excluded from `rag_search` by
default alongside profile memories (the `include_memories` preference opts
both back in). Team memory dies with the team on delete.

```http
GET /thread-teams
Authorization: Bearer <token>
```

Returns `{"teams": [{"id": "...", "name": "...", "description": ... | null, "thread_ids": [...]}], "total": N}` for the authenticated user's thread teams. Teams with no members are included (empty teams are legal).

```http
POST /thread-teams
Authorization: Bearer <token>
Content-Type: application/json

{"name": "Ops", "description": "Ops helpers", "thread_ids": ["thread-a", "thread-b"]}
```

Creates a team, optionally moving the listed owned threads into it. `description` is optional; `thread_ids` may be empty (create the entity now, add members later). 409 on a name collision (case-insensitive).

```http
PATCH /thread-teams/{team_id}
Authorization: Bearer <token>
Content-Type: application/json

{"name": "Ops Team", "description": "...", "thread_ids": ["thread-b", "thread-c"]}
```

Renames/describes a team and/or replaces its membership; all fields optional. Moving a thread into a team removes it from any previous callable team. `thread_ids: []` unteams every member but keeps the team entity; `description: ""` clears the description. A rename or description edit alone writes only the team store: no member config writes, no graph invalidation.

```http
DELETE /thread-teams/{team_id}
Authorization: Bearer <token>
```

Deletes a team: clears membership from its threads and removes the entity. Membership changes invalidate cached graphs for the user's threads so subsequent turns rebuild callable-tool visibility.

```http
GET /thread-teams/{team_id}/memories
Authorization: Bearer <token>
```

Returns `{"team_id": "...", "name": "...", "memories": [{"key": "...", "value": "...", "created_at": "...", "updated_at": "..."}], "count": N}`, sorted by key. Read-only: a dangling membership-referenced id renders without being adopted into the store.

```http
POST /thread-teams/{team_id}/memories
Authorization: Bearer <token>
Content-Type: application/json

{"key": "api_endpoint", "value": "https://stage.example.com"}
```

Upserts one shared entry. The value is truncated to the per-value cap and the per-team aggregate budget is enforced (the global memory caps, applied per team); a cap violation is HTTP 400. 404 for an unknown team.

```http
DELETE /thread-teams/{team_id}/memories/{key}
Authorization: Bearer <token>
```

Deletes one shared entry (404 if the team or key does not exist).

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
| `callable_team_id` | string | Optional callable visibility team id (`""` unteams). Membership source of truth; the store adopts unknown ids as new entities |
| `callable_team_name` | string | DEPRECATED write field: ignored on PATCH (renames go through the teams API). Config responses still SERVE it, derived from the team store |
| `custom_instructions` | string | System prompt for this thread |
| `disabled_tools` | array | Tool names to exclude |
| `enabled_tools` | array | Optional tool names to include |
| `llm_provider` | string | Override LLM provider |
| `llm_model` | string | Override model |
| `llm_config.base_url` | string | Per-thread provider base URL. For CLIProxy sidecars, this is the URL reachable from the Nymeria backend container, e.g. `http://cli-proxy-api-latest:8317/v1`. The server's stored provider credential only follows this address when the deployment is already configured for it (it matches `LLM_BASE_URL`, `LLM_BACKGROUND_BASE_URL`, or the provider's own configured base URL; the provider's canonical vendor host counts only when none of those are set). Any other address is ignored, for every account including admins, unless the thread also sets its own `llm_config.api_key`: the turn runs on the configured provider instead and the thread is told once via a `fallback_notice` history entry carrying `note_kind: "destination"`. A `${credential:...}` reference in `api_key` counts as the caller's own only when it names a record the caller owns. The rule is that the DESTINATION may come from the request but the server's CREDENTIAL may not, the same principle as `GET /models/available`. |
| `llm_config.api_key` | string | Per-thread provider API key. For CLIProxy sidecars, this is the local sidecar gatekeeper key, not an upstream OpenAI key. |
| `llm_config.context_length` | int | Per-thread context-window override for local endpoints or proxies with missing metadata |
| `llm_config.ollama_num_ctx` | int | Per-thread Ollama `options.num_ctx` override |
| `llm_config.provider_route` | string | Per-thread adapter route override: `native`, `openai_compat`, or `anthropic_messages`. Only applies to providers whose catalog row advertises multiple `supported_routes` (`anthropic_messages` routes a gateway's Claude models through langchain-anthropic for native thinking). |
| `llm_config.fallback_switch_mode` | string | Per-thread override of `llm_fallback_switch_mode` (`auto`/`ask`); null inherits the global. |
| `llm_config.refusal_swap_mode` | string | Per-thread override of `llm_refusal_swap_mode` (`off`/`ask`/`auto`); null inherits the global. |
| `clear_active_fallback` | bool | Revert an active fallback hold (the GUI chip / Model-tab Revert, mirroring `/fallback revert`): clears `active_llm_fallback` and latches a model-facing end note for the thread's next turn. |
| `telegram_autonomous_delivery` | `"full" \| "notify_only" \| "off"` | Telegram delivery for autonomous outputs. Default `full`. |
| `in_app_notification_level` | `"notify_only" \| "all_autonomous" \| "off"` | Notification-center behavior. Default `notify_only`. |
| `llm_temperature` | float | Override temperature |
| `llm_config.openai_api_mode` | string | OpenAI-compatible API mode: `chat_completions` or `responses`. Use `responses` for CLIProxy Codex OAuth threads that need native Responses reasoning/tool blocks replayed from the checkpoint. |
| `memory_char_limit` | int | Optional per-thread notepad character limit. Omit or clear to inherit the global `MEMORY_CHAR_LIMIT` default. |
| `dreaming.enabled` | bool | Opt in to background self-reflection for this thread. Default `false`. Not inherited: dreaming is always opt-in per thread. |
| `dreaming.min_interval_hours` | int | Minimum hours between scheduled dream runs. Blank/omitted inherits the global default (`dream_default_min_interval_hours`, default `6`). |
| `dreaming.min_idle_minutes` | int | Minimum idle time before a scheduled dream can start. Blank/omitted inherits the global default (`dream_default_min_idle_minutes`, default `30`). |
| `dreaming.min_turns_since_last` | int | Minimum parent-thread turn count before the next scheduled dream. Blank/omitted inherits the global default (`dream_default_min_turns_since_last`, default `10`). |
| `dreaming.model` | string | Optional model override for dream turns. Blank/omitted inherits the global default (`dream_default_model`), then the global active model. |
| `dreaming.system_prompt` | string | Optional per-thread override of the dream system prompt. Blank/omitted falls back to the global default (`GET/PUT /settings/dream-prompts`). |
| `dreaming.kickoff_prompt` | string | Optional per-thread override of the dream kickoff message (first message sent to the dreaming thread). Supports `{parent_thread_id}` and `{parent_instructions}` placeholders. Blank/omitted falls back to the global default. |

**Callable thread naming:** A callable thread's sidebar title is derived from
`callable_name`. Rename the callable tool binding by updating `callable_name`
through `PATCH /threads/{thread_id}/config`; `PATCH /threads/{thread_id}/metadata`
only updates display metadata for non-callable threads.

```http
POST /threads/{thread_id}/dream
Content-Type: application/json
Authorization: Bearer <token>
```

Manually starts one dream cycle for a configured parent thread. The endpoint
creates a temporary shadow thread, binds the strict dream tool allowlist, runs
the dream turn in the background, and returns the shadow thread id immediately.
It respects `dreaming.enabled`; pass `{"force": true}` only for admin/debug
one-off runs.

Request fields:

| Field | Type | Description |
|-------|------|-------------|
| `model` | string | Optional one-run model override. Defaults to `dreaming.model`, then the global `dream_default_model`, then the normal active model. |
| `force` | bool | Bypass the `dreaming.enabled` opt-in check. Default `false`. |

Response fields:

| Field | Type | Description |
|-------|------|-------------|
| `shadow_thread_id` | string | Temporary dream thread id. |
| `parent_thread_id` | string | Parent thread that the dream is reflecting on. |
| `started_at` | string | UTC timestamp for the run setup. |
| `model` | string | Model selected for the dream turn. |
| `enabled_optional_tools` | array | Optional dream-policy tools enabled on the shadow thread. |
| `disabled_core_tools` | array | Core tools explicitly blocked by dream policy context. |

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

## Skills API

Agent skills are markdown bundles managed by the `SkillManager`. The API
supports installed-skill discovery, marketplace search/install, uninstall, and
effective skill resolution for a thread.

### List Installed Skills

```http
GET /skills?scope=user
Authorization: Bearer <token>
```

Lists skills visible to the effective user. `scope` is optional and can be
`user`, `global`, or `bundled`.

### Search Skill Marketplace

```http
GET /skills/marketplace/search?source=anthropic&q=calendar
Authorization: Bearer <token>
```

Searches a marketplace source and returns matching skill metadata. Unsupported
sources return `501`; marketplace fetch failures return `502`.

### Get Skill Detail

```http
GET /skills/{name}
Authorization: Bearer <token>
```

Returns skill metadata, full markdown body, path, license, scripts, and
references for an installed skill.

### Install Skill

```http
POST /skills/install
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "name": "calendar",
  "source": "anthropic",
  "scope": "user"
}
```

`scope=user` installs for the caller. `scope=global` is admin-only because a
skill bundle can include scripts and shared assets.

### Uninstall Skill

```http
DELETE /skills/{name}?scope=user
Authorization: Bearer <token>
```

Removes the skill from user or global scope. Global uninstall is admin-only.

### Thread Active Skills

```http
GET /threads/{thread_id}/skills
Authorization: Bearer <token>
```

Returns global-enabled, thread-enabled, thread-disabled, and resolved active
skills for the thread after ownership and scope checks.

### Global Skill Defaults

```http
GET /settings/global-skills
PUT /settings/global-skills
Authorization: Bearer <token>
```

The `PUT` body is `{"skill_names":["skill-a","skill-b"]}` and replaces the
effective user's enabled-by-default skill list.

---

## User Memories API

Profile memories are keyed text facts stored on a user profile. Path `user_id`
must match the effective authenticated user; admins target another user with
`X-Nymeria-Act-As`.

### List Memories

```http
GET /users/{user_id}/memories
Authorization: Bearer <token>
```

Returns memory key/value rows with creation/access metadata.

### Save Memory

```http
POST /users/{user_id}/memories
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "key": "timezone",
  "value": "America/New_York"
}
```

Creates or updates the memory and syncs a memory chunk into the user's RAG
index when RAG is available. The saved profile memory set must fit within
`MEMORY_CHAR_LIMIT` characters when rendered as `key: value` rows; over-limit
growth returns `400` with a memory-full error so the caller can consolidate or
delete older memories.

### Delete Memory

```http
DELETE /users/{user_id}/memories/{key}
Authorization: Bearer <token>
```

Removes the memory and its RAG memory chunk.

### Search Memories

```http
GET /users/{user_id}/memories/search?q=timezone
Authorization: Bearer <token>
```

Searches profile memories by key or value substring.

---

## RAG Management API

Manage RAG (Retrieval Augmented Generation) settings and indexes per user.

`rag_enabled` defaults to `true` as of 2026-04. Existing profiles created before that are migrated once on load (watermarked by `opt_in.rag_migrated`). To disable, set `rag_enabled=false` via this API or the frontend settings UI  -  the watermark prevents re-flipping.

Conversation indexing happens automatically in four places: per turn, before `/compact` (manual + auto), before `/threads/{id}/clear`, and thread chunks are removed as part of the full `DELETE /threads/{id}` cascade. Saved profile memories are also synced into the memory chunk index when created or updated through the REST API or agent tools, and removed from the index when forgotten. See `architecture.md` → "RAG (Semantic Conversation Recall)".

### Get RAG Settings

```http
GET /users/{user_id}/rag/settings
Authorization: Bearer <token>
```

**Response:** (flat object; these are this user's own RAG settings)
```json
{
  "enabled": true,
  "max_chunks": 5,
  "include_conversations": true,
  "include_memories": false,
  "include_todos": true,
  "include_tools": true,
  "auto_flush": true,
  "retrieval_mode": "hybrid",
  "rerank_enabled": false
}
```

`retrieval_mode` (`hybrid` or `vector`) and `rerank_enabled` are per-user overrides of the server defaults (`RAG_RETRIEVAL_MODE` / `RAG_RERANK_ENABLED`); the rest are per-user content preferences. The shared embedder and reranker engine are admin-only server settings (see PATCH /settings).

---

### Update RAG Settings

```http
PUT /users/{user_id}/rag/settings
Content-Type: application/json
Authorization: Bearer <token>
```

**Request Body:** (all fields optional; only the ones sent are changed)
```json
{
  "enabled": true,
  "max_chunks": 5,
  "include_conversations": true,
  "include_memories": false,
  "include_todos": false,
  "include_tools": true,
  "auto_flush": true,
  "retrieval_mode": "vector",
  "rerank_enabled": true
}
```

**Response:** Updated settings object (same shape as GET)

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
      "enable_hint": "/tools enable browser_open",
      "auth_status": null,
      "auth_provider": null
    }
  ]
}
```

`mode` is one of `semantic`, `bm25`, `fuzzy`, or `substring`. `warning` is
`null` when semantic search is active or no fallback warning is needed.
`auth_status`/`auth_provider` form the credential axis for provider-mapped
tools (`connected` / `pending` / `needs_setup` / `optional`, vault metadata
only); both are `null` for tools that need no credential. The same two
fields appear on `GET /tools/defaults` items and unified-tool responses.
Managed MCP results (`tool_type` `mcp_server`) additionally carry
`server_id`, `server_name`, and `display_name` (a compact `server / tool`
label; the `name` stays the clean internal `mcp__server__tool`), and reuse
`auth_status` as a setup axis mapped from the server's install status
(`ready` -> `connected`, `needs_config` -> `needs_setup`) with
`auth_provider` `null` (MCP has no credential provider). These same MCP
fields appear on `GET /tools/defaults` items and unified-tool responses.

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
tool configuration updates live. Path `user_id` must match the effective
authenticated user; admins target another user with `X-Nymeria-Act-As`.

### List Unified Tools

```http
GET /users/{user_id}/tools/unified
Authorization: Bearer <token>
```

Returns all visible tools in a unified format. Custom tool definitions include
HTTP/MCP configuration, so they are only returned to admin users. The response
uses `tool_type` values `builtin`, `mcp_server`, or `custom`, plus
`builtin_count` and `custom_count`. Each tool also carries the credential
axis fields `auth_status`/`auth_provider` (see the tool-search endpoint
above); tools that need no credential return `null` for both. `mcp_server`
tools additionally carry `server_id`/`server_name`/`display_name` provenance
and reuse `auth_status` as a setup axis from the server's install status (see
the tool-search note above).

### Enable Unified Tool

```http
PUT /users/{user_id}/tools/unified/{tool_id}/enable
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "enabled": true
}
```

Enable or disable a built-in or live MCP server tool by mutating the user's
`default_thread_tools`. Role-gated tools still require an admin caller. Custom
tool execution is controlled through thread `enabled_tools` and custom-tool
definition state, not this endpoint.

### Update Unified Tool Description

```http
PUT /users/{user_id}/tools/unified/{tool_id}/description
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "description": "Updated description for the tool"
}
```

Overrides the user-visible description for a built-in or custom tool. Passing
`{"description": null}` clears the override.

### Update Unified Tool Config

```http
PUT /users/{user_id}/tools/unified/{tool_id}/config
Content-Type: application/json
Authorization: Bearer <token>
```

```json
{
  "config": {
    "timeout_seconds": 30,
    "custom_option": "value"
  }
}
```

Sets per-user runtime config for a built-in or custom tool. Passing an empty
config object clears existing config.

**List Response Example:**
```json
{
  "tools": [
    {
      "id": "bash_execute",
      "name": "bash_execute",
      "description": "Execute shell commands...",
      "tool_type": "builtin",
      "category": "general",
      "enabled": true,
      "parameters": null
    },
    {
      "id": "get_weather",
      "name": "Get Weather",
      "description": "Get current weather...",
      "tool_type": "custom",
      "category": "custom",
      "enabled": true,
      "parameters": {...}
    }
  ],
  "total": 48,
  "builtin_count": 40,
  "custom_count": 1
}
```

---

### Create Unified Custom Tool

```http
POST /tools/unified
Content-Type: application/json
Authorization: Bearer <admin-token>
```

Creates a custom HTTP or MCP tool and reloads the tool registry. The request
body matches `POST /tools/custom`.

### Update Unified Custom Tool

```http
PUT /tools/unified/{tool_id}
Content-Type: application/json
Authorization: Bearer <admin-token>
```

Updates an existing custom tool and reloads the tool registry. Built-in tools
cannot be edited through this route.

### Delete Unified Custom Tool

```http
DELETE /tools/unified/{tool_id}
Authorization: Bearer <admin-token>
```

Deletes a custom tool definition and reloads custom tools. Built-in tools
cannot be deleted.

**Delete Response:**
```json
{
  "status": "ok",
  "deleted": "get_weather"
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
- `/voice/tts` accepts JSON `{"text": "...", "voice_note": false}` and returns
  synthesized audio. `voice_note: true` requests a chat-platform voice-message
  container (Ogg/Opus or MP3); check the response `Content-Type` for what was
  produced. Used by the Telegram bot for voice-note replies.
- `/voice/stt` accepts audio and returns transcribed text.
- Errors: `503` when the provider is not configured (`TTS_PROVIDER`/`STT_PROVIDER`),
  `502` when the provider call fails.

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
    "general": ["bash_execute", "file_read", "file_write", "web_search_perplexity", "consult", "notify"],
    "profile": ["memory_add", "memory_edit", "memory_read", "personality_set", "rag_search"],
    "todo": ["nym_todo", "nym_todo_delete", "nym_todo_list"],
    "skills": ["skill_manage", "list_installed_skills", "search_skills", "install_skill"],
    "mcp_server": ["manage_mcp", "search_mcp", "install_mcp_server"],
    "custom": ["tool_search", "tool_manage", "api_discover", "http_request", "tool_create", "skill_write", "skill_edit"],
    "self_modify": ["self_modify_rollback", "reload_all"],
    "thread_spawn": ["spawn_thread"]
  }
}
```

Capability-expansion categories are optional by default; the bundled,
default-on `tool-management`, `skill-management`, and `mcp-management` Skill Kits
bind the consolidated facades when activated, and `self-improve` is text-only
guidance that routes to them.

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

`action_type` is one of `agent_prompt` (render `prompt_template` and run an
agent turn), `notify`, `create_todo`, or `run_workflow`. A `run_workflow`
action runs a published workflow tool headlessly (no LLM call):
`action_config` takes `workflow_id` plus optional `params`, the binding is
validated at create/update time (400 on a missing tool, unapproved revision,
unknown or uncovered required parameters), and at fire time the raw event
dict is passed as the workflow's `event` parameter when its signature
declares one. A run that suspends on `nym.approve` counts as a successful
fire; an error envelope records a failed execution.

### List Triggers

```http
GET /triggers?enabled_only=false&thread_id=<optional>
Authorization: Bearer <token>
```

**Query parameters:**
- `enabled_only` (`bool`, default `false`)  -  return only enabled triggers
- `thread_id` (`str`, optional)  -  filter to triggers whose action targets the given thread

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

## Hooks API

Lifecycle hooks that run a canned action when an event fires. Actions:
`inject_context` (inject a string, on `prompt_submit`/`post_tool_use`/`done`),
`block_if_matches` (deny a tool call, `pre_tool_use`), `rewrite_arg` (modify a
tool call's args, `pre_tool_use`), `require_approval` (hold a tool call for the
user's approve/deny decision, `pre_tool_use`; no answer within the window
denies it), and the observe-plane side effects `notify`
(in-app + push notification), `create_todo` (add a TODO), and `webhook` (POST to
a URL) on `post_tool_use`/`done`. Hooks fire in-process only, so unlike triggers
there is no public fire/webhook endpoint. See
`docs/agent-systems/hooks.md`. All routes require a Bearer token and
operate on the authenticated user's own hooks.

Every list also carries the reserved SYSTEM hook `turn-metadata` (`system:
true` on the payload, action `turn_metadata`): the built-in `[Time:]/[Trigger:]`
turn-metadata block as an editable hook. It exists virtually until first edited
(PATCH materializes it), `event`/`scope`/`action`/`single_use` are locked (400),
its `text` template must keep the fixed two-line `[Time: ...]`/`[Trigger: ...]`
frame, and DELETE means "reset to built-in defaults" (204 even when already
pristine, log entries kept). The `turn_metadata` action is not creatable
(reserved); `GET /hooks/schema` marks it `system: true`.

### List Hooks

```http
GET /hooks?enabled_only=false&thread_id=<optional>
Authorization: Bearer <token>
```

**Query parameters:**
- `enabled_only` (`bool`, default `false`) - return only enabled hooks
- `thread_id` (`str`, optional) - return global hooks plus those bound to this thread

### Create Hook

```http
POST /hooks
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{
  "name": "Remind on edit",
  "event": "post_tool_use",
  "action": "inject_context",
  "text": "Re-run the tests after editing {tool_name}.",
  "matcher": "Edit|Write",
  "scope": "thread",
  "thread_id": "default",
  "enabled": true
}
```

A `pre_tool_use` guardrail instead sends `action` plus per-action fields:

```json
{
  "name": "Block rm -rf",
  "event": "pre_tool_use",
  "action": "block_if_matches",
  "matcher": "bash",
  "conditions": [{"field": "command", "operator": "contains", "value": "rm -rf"}],
  "reason": "Destructive command blocked by a hook.",
  "scope": "global"
}
```

`event` is one of `prompt_submit`, `pre_tool_use`, `post_tool_use`, `done`;
`action` is `inject_context` (default), `block_if_matches`, `rewrite_arg`,
`require_approval`, `notify`, `create_todo`, `webhook`, or `run_command`, and
must be legal for the
event (`notify`/`create_todo`/`webhook` are `post_tool_use`/`done` only;
`require_approval` is `pre_tool_use` only;
`run_command` is legal on all four). `matcher` (a pipe-list tool-NAME filter)
applies to the tool events (`pre_tool_use`/`post_tool_use`) and is dropped on
others. Per-action fields: `text` (inject_context / notify / create_todo, and
the webhook body, `{placeholder}` interpolated); `conditions` + `reason`
(block_if_matches); `conditions` + `updates` (rewrite_arg); `conditions` +
`text` (the approval prompt) + `timeout_seconds` (require_approval, window
10..600s, default 180); `url` + `text`
(webhook); `command` + `timeout_seconds` (run_command).

**`run_command` is gated.** It runs a shell command on the host, so it is
admin-only AND requires the `HOOKS_RUN_COMMAND_ENABLED` deployment flag.
Authoring (creating, or `PATCH`ing an existing hook to) `run_command` returns
`403` for a non-admin caller and `400` when the flag is off. See
`docs/agent-systems/hooks.md` for its stdin/env/timeout contract.
`conditions` match the tool call's args (operators `equals`,
`not_equals`, `contains`, `starts_with`, `matches_regex`; `field` supports dotted
paths). `scope` is `thread` (bound to `thread_id`) or `global` (all the user's
threads). The body also accepts the lifecycle flag `single_use` (bool, default
false: the hook deletes itself after its first successful run, log entries
kept). Returns `201` with the created hook (whose `logic` object holds the
full action config), `400` on invalid config or when the per-user cap (50) is
reached. Creating a thread-scoped hook for a thread the caller cannot access is
rejected.

### Get / Update / Delete Hook

```http
GET    /hooks/{hook_id}
PATCH  /hooks/{hook_id}
DELETE /hooks/{hook_id}
Authorization: Bearer <token>
```

`PATCH` accepts any subset of `name`, `event`, `action`, `matcher`, `enabled`,
`single_use`,
and the per-action logic fields (`text` / `conditions` / `reason` / `updates` /
`url` / `command` / `timeout_seconds`); the logic is rebuilt and re-validated on
save. Switching `action` TO `run_command` is gated exactly as create (403
non-admin / 400 flag-off), and so is any behavior edit of an existing
run_command hook (anything beyond `enabled`/`name`): authoring-time admin is
not a permanent pass. Enabled/name-only updates stay ungated so the owner can
always toggle or rename. `scope`/`thread_id` are
deliberately not patchable (every authoring surface enforces this): a re-scope
needs a thread binding and its access gate, so it is a delete + create.
`DELETE` returns `204` and purges the hook's execution-log entries. All return
`404` if the hook does not exist for the authenticated user. The system
`turn-metadata` hook is the exception: DELETE resets it to built-in defaults
(idempotent `204`, log kept) and it can never 404 on GET.

### Test Hook (Dry Run)

```http
POST /hooks/{hook_id}/test
Authorization: Bearer <token>
```

Previews the hook against sample event data without firing: `inject_context`
renders its template, the guardrail actions describe what they would do. Returns
`{"hook_id", "event", "action", "rendered"}`.

### Hook Templates

```http
GET  /hooks/templates
POST /hooks/templates/{template_id}/install
Authorization: Bearer <token>
```

`GET` lists the bundled hook-template catalog (curated JSONs shipped in
`nymeria/hooks_bundled/`, dry-validated at load): each entry carries
`{id, title, description, notes, hook}`, where `hook` is the full definition
body the install would create (event, action, fire gate, default scope).
`POST` installs one as a regular hook for the caller; optional body fields
`{"scope": "thread"|"global", "thread_id": "...", "text": "...",
"enabled": bool}` override the template defaults (a thread-scoped install
requires `thread_id`, and a supplied `thread_id` is access-checked
whenever present). Installs are idempotent per (template, scope, thread
binding): the response is `{"created": bool, "hook": <hook>}`, with
`created=false` returning the existing installation. The installed hook
records its origin in the response's `template` field and is otherwise an
ordinary hook (edit/disable/delete as usual). Gated actions (`run_command`)
pass the same authoring gates as a manual create. `400` on an unknown
template id.

### Hook Executions

```http
GET /hooks/executions?hook_id=<optional>&limit=50
Authorization: Bearer <token>
```

Recent hook executions, newest first (bounded per-user log, cap 200, written
behind the turn so recording adds no in-band latency). Each entry:
`{id, hook_id, hook_name, event, plane, status, detail, duration_seconds,
thread_id, tool_name, timestamp}`. `status` is `ok` (ran, produced an outcome
or side effect; `detail` summarizes it, e.g. `deny: <reason>` or
`inject 84 chars`), `no_op` (ran, produced nothing), `error`, `timeout`
(execution overran the per-hook budget), `saturated` (never got a dispatch
worker), or `illegal` (returned the wrong outcome type; dropped). On
`pre_tool_use` an `error`/`timeout`/`saturated` run also denied the tool call
(the fail-closed policy). Observe-plane runs record `ok` on success, never
`no_op` (their return values are ignored). A hook with no entries never fired.
Also surfaced as `/hook history [id] [--limit N]` and `hook_info(action="log")`.

### Pending Approvals

```http
GET /hooks/approvals
Authorization: Bearer <token>
```

Pending `require_approval` holds: tool calls paused for a decision. Admins see
every user's pending holds; everyone else sees their own. Returns
`{"approvals": [...]}`, each entry
`{record_id, user_id, thread_id, hook_id, hook_name, tool_name, tool_call_id,
tool_args_preview, prompt, is_autonomous, created_at, expires_at}`. A hold not
resolved by `expires_at` denies the tool call.

### Resolve Approval

```http
POST /hooks/approvals/{record_id}/resolve
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{"approved": true, "note": "optional, shown to the agent on deny"}
```

Approve or deny a held tool call (owner or admin; the resolver's identity is
recorded as `resolved_by`). Returns `{"ok", "record_id", "decision"}`. `404`
covers a missing record, another user's record (existence is not leaked), and
the common already-ended cases (timed out and denied, resolved elsewhere, or
its turn died: the waiting action removes its record on every exit). `409` is
the rarer stale shape: the record still exists but no waiter is parked on it
(typically a crash orphan surviving a restart); stale records are cleaned up
on the spot. Every resolution
(including timeout and turn abort) publishes a `hook_approval_resolved` SSE
event so all surfaces retract their prompt. The `/hook approvals`,
`/hook approve <id> [note]`, and `/hook deny <id> [note]` slash commands are a
command-surface front for these endpoints (agent-denied: the agent can never
approve its own held calls).

### Authoring Schema

```http
GET /hooks/schema
Authorization: Bearer <token>
```

The machine-readable authoring taxonomy, derived from the backend single
source (`core/hook_spec.py`): per-event legal actions and tool-event flag,
per-action `plane`/`events`/`text_action`, a `plane_by_event` map (which
event dispatches on which plane; `run_command` flips mutate-to-observe across
its four events), a `gated` flag (true for admin + flag-gated actions, i.e.
`run_command`), `params_schema` (the action's JSON schema minus the `action`
discriminator), the condition `operators`, and `max_hooks`. Clients can render
authoring forms from this instead of hardcoding the legality map.

### Enable model

A hook fires on a turn only if the master switch, any per-thread override, and
the hook's own flag all allow it:

- **Master switch**: `hooks_enabled` on `PATCH /settings` (global), overridable
  per thread by `hooks_enabled` on `PATCH /threads/{id}/config`.
- **Per-thread per-hook override**: `hook_overrides` (a `{hook_id: bool}` map)
  on `PATCH /threads/{id}/config`; `clear_hook_overrides` resets it.
- **Per-hook default**: the `enabled` flag on the hook record.

---

## LLM Fallback Consent Prompts

When `LLM_FALLBACK_SWITCH_MODE=ask` (or `LLM_REFUSAL_SWAP_MODE=ask`, the
default since the consent card shipped), a
consent-capable interactive turn parks before a model switch and publishes a
`fallback_prompt` event. These endpoints are the list/resolve surface (the
`/fallback approvals|approve|deny` slash commands front them; both are
human-only, the agent cannot resolve its own parked switch).

Every applied swap also leaves a model-facing note IN the conversation
(persisted, never repeated): mid-turn it is appended to the last tool result,
on a first-call switch to the prompt message itself, and when a hold ends
(expiry or any revert surface) the next turn carries a back-on-primary note.
History renders these as `fallback_notice` system entries (see Conversation
History).

### Pending Fallback Prompts

```http
GET /llm/fallback-approvals
Authorization: Bearer <token>
```

Pending parked switches. Admins see every user's; everyone else sees their
own. Returns `{"approvals": [...]}`, each entry
`{record_id, kind, user_id, thread_id, from_provider, from_model, to_provider,
to_model, reason, http_status, timeout_seconds, hold_options, allow_permanent,
default_hold_seconds, is_autonomous, created_at, expires_at}`. A prompt not
resolved by `expires_at` AUTO-SWAPS with the default hold (a fallback is a
resilience action; contrast hook approvals, which deny on timeout).

### Resolve Fallback Prompt

```http
POST /llm/fallback-approvals/{record_id}/resolve
Authorization: Bearer <token>
Content-Type: application/json
```

```json
{"approved": true, "hold_seconds": 3600, "hold_permanent": false, "note": ""}
```

Approve (swap, optionally choosing the hold: `hold_seconds` from
`hold_options`, or `hold_permanent: true` for an until-manually-reverted hold;
omitted = the default hold) or decline the switch (owner or admin). On a
`transport` prompt a decline fails the turn with the original provider error;
on a `refusal` prompt a decline falls through to the rewind-and-restore path.
`404` covers a missing record, another user's record, and the common
already-ended cases (timed out and auto-swapped, resolved elsewhere, or its
turn died: the parked waiter removes its record on every exit). `409` is the
rarer stale shape: the record still exists but no waiter is parked on it
(typically a crash orphan surviving a restart); stale records are cleaned up
on the spot. Every resolution publishes `fallback_prompt_resolved`. An active hold is inspectable and
revertible via `/fallback status` and `/fallback revert` (permanent holds
require the revert).

## CLIProxy Management API

Admin-only routes for driving a CLIProxy sidecar (subscription OAuth) through
its remote-management API. All routes require an admin account token and are
inert until `CLIPROXY_MANAGEMENT_URL` and `CLIPROXY_MANAGEMENT_KEY` are set
(see the configuration doc). The provider catalog is the single source of
route shapes; frontends never derive base URLs or key slots themselves.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/cliproxy/catalog` | Static provider catalog (id, label, flow, route shape, default model, ToS warning) |
| GET | `/cliproxy/status` | Reachability plus per-provider support (live probe, cached 15 min; `?refresh=true` re-probes) and login state. Degrades to `configured/reachable: false` instead of erroring |
| POST | `/cliproxy/oauth/start` | `{provider}` -> `{url, state, flow}`; open `url` in any browser |
| GET | `/cliproxy/oauth/status?state=&provider=` | Poll the pending login: `{status: wait\|ok\|error, detail}`. `ok` is server-CONFIRMED against the auth-file list (the proxy answers a bare ok for unknown or expired sessions, so an unconfirmed ok comes back as `error` with the trap explained in `detail`); a confirmed ok carries the account label in `detail`, and for Claude re-asserts `tool_prefix_disabled` on the auth file |
| POST | `/cliproxy/oauth/callback` | `{provider, redirect_url}` (or `code`+`state`): deliver a browser callback that landed on a dead localhost page |
| GET | `/cliproxy/auth-files?provider=` | List the proxy's stored logins with status |
| POST | `/cliproxy/auth-files` | Import an `auths/*.json` document: `{provider, name, content}` (content = the file's JSON text). Uploads, then CONFIRMS the proxy lists the UPLOADED name as an active login for the provider (the confirm-on-ok trust rule; a pre-existing login can never bless a dead or mismatched import): `{status: "ok", account}` or `{status: "inactive", detail}` when the proxy accepted the file but does not list it as an active login; a confirmed Claude import also gets the `tool_prefix_disabled` fixup |
| PATCH | `/cliproxy/auth-files/{name}` | `{disabled?, priority?}` |
| DELETE | `/cliproxy/auth-files/{name}` | Remove a login from the proxy |
| GET / PATCH | `/cliproxy/config` | The surfaced knob subset (`api-keys`, `request-retry`, `max-retry-interval`, `routing/strategy`, `oauth-model-alias`, `oauth-excluded-models`, `quota-exceeded/*`) |
| GET | `/cliproxy/models` | Live `{models: [{id, owned_by}]}` from the proxy's `/v1/models`, authenticated with the first configured gatekeeper key READ through the management API (a key-less proxy has an open data plane, so the request then goes out unauthenticated; a read never mints); lists every logged-in provider's models |
| POST | `/cliproxy/verify` | `{provider, model?}` (model defaults to the provider spec's). Prove a logged-in subscription actually serves traffic: one real data-plane completion through the same probe as `/settings/llm/test`, using the gatekeeper read through the management API (never minted, never returned). `{verdict, detail}` where verdict is `ok` (reached the upstream; `detail` is the probed model), `auth_failed` (401/403, the upstream rejected the credential whatever the auth-file list says), or `inconclusive` (proxy down, model unknown, 5xx, timeout: NOT a failure, since a transient fault must not invalidate a good login). A 429 counts as `ok`, because it proves the request reached the upstream and this probe carries no OAuth billing fingerprint. Complements `/cliproxy/oauth/status`, which only confirms the proxy LISTS an enabled auth file |
| POST | `/cliproxy/apply-route` | `{provider, model?, scope: global\|thread, thread_id?, gatekeeper_key?}`: turn a catalog entry into LLM settings. Global scope hot-reloads through the settings applier; thread scope writes the per-thread LLM config. Gatekeeper resolution order: request `gatekeeper_key` -> the settings key IF it is `cpx-`-shaped or matches the proxy's `api-keys` list (a real provider key, e.g. an `sk-` OPENAI_API_KEY on a codex route, is never adopted as the gatekeeper when the proxy has its own keys) -> the proxy's first configured key, or mint-and-write a `cpx-nymeria-*` key via the management API -> only then 422 |

Status codes: 400 (management not configured / bad request), 404 (unknown
provider or auth file), 409 (proxy state conflict), 422 (provider unsupported
by the pinned proxy binary, or gatekeeper resolution failed against both
settings and the management API), 502 (proxy unreachable or management key
rejected).

The `/provider cliproxy` slash command drives this whole surface as a chained
in-REPL flow (overview with logged-in badges, OAuth login with paste/status
steps, model pick from `/cliproxy/models`, route apply); it is excluded from
chat-bot surfaces and agents because pasted authorization codes must not
persist in platform chat history.

## Error Responses

All errors follow this format:

```json
{
  "detail": "Error message here"
}
```

| Status Code | Meaning |
|-------------|---------|
| 401 | Missing, invalid, or revoked bearer token |
| 403 | Authenticated caller is not allowed to perform the action |
| 404 | Resource not found or not visible to the effective user |
| 409 | Resource state conflict, such as a busy thread or executing TODO |
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
