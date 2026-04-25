# Accounts & Authentication

Nymeria is transitioning from implicit-single-user to a real multi-user model. This doc covers the account/token layer (Step 1 of the rollout). Thread-ownership enforcement, the `require_user` middleware, and bot platform mapping land in later steps — see `ROADMAP.md`.

## Model

Four tables in a dedicated SQLite database at `<data_dir>/accounts.db`:

| Table | Purpose |
|---|---|
| `users` | Accounts (`id`, `email`, `display_name`, `role`, `disabled`) |
| `user_tokens` | Bearer tokens (one user can have many, each independently revocable) |
| `thread_owners` | Maps `thread_id → user_id`; populated on first-touch (Step 5) |
| `platform_identities` | Maps Discord/Telegram/Twitch user IDs to Nymeria accounts (Step 6) |

Roles: `user` and `admin`. Admins can use the `X-Nymeria-Act-As` header (Step 3) to call the API on behalf of another user — used by bots, the ticker, and the watchdog.

### Tokens

Raw token format: `nym_<32-url-safe-bytes>`. Only `sha256(raw)` is stored. The raw value is returned **once** at creation and cannot be recovered — lost tokens must be rotated.

Token redaction is applied at the logging layer (`config/logging_config.py::_TokenRedactingFilter`), so accidentally logged tokens come out as `nym_<redacted>`.

## Bootstrap

On first run with an empty `users` table, the agent auto-creates a default admin:

- `id`: `default`
- `email`: `owner@localhost`
- `display_name`: `Owner`
- `role`: `admin`

The raw token is:
1. Logged at WARNING level with a loud banner
2. Written to `<data_dir>/BOOTSTRAP_TOKEN.txt` (mode 0600)

Paste the token into the Desktop/Mobile Setup Wizard in place of the old `NYMERIA_API_KEY`, then delete the file. The `default` user ID lines up with existing per-user file paths (`data/todos/default.json`, `data/profiles/default.json`, etc.) so no data migration is needed for the first user.

## CLI (legacy — unmaintained)

> **Status:** the `users` CLI predates the HTTP Admin API and is no longer actively tested. Prefer the HTTP endpoints (next section) for anything beyond first-boot bootstrap. The CLI is kept around because it operates directly on the accounts DB and so still works when the API is down.

```bash
# Create a user, mint a token
python run.py users add bob@example.com --role user --id bob --display-name "Bob"
# prints: Token: nym_...   (shown once)

# List users
python run.py users list

# Temporarily disable / re-enable
python run.py users disable bob@example.com
python run.py users enable bob@example.com

# Revoke every token, mint a fresh one
python run.py users rotate-token bob@example.com

# Link a Discord/Telegram/Twitch identity to a user (enables bot routing)
python run.py users link-platform bob@example.com discord 123456789
python run.py users platforms bob@example.com
python run.py users unlink-platform discord 123456789
```

Inside Docker: `docker exec nymeria-api python run.py users <action>`.

## HTTP Admin API

Every account operation is exposed as a REST endpoint, gated by `require_admin_user`. Pass an admin token (the bootstrap token works for first-time setup) as `Authorization: Bearer nym_...`. Raw tokens are returned **once**, in the response body of a creation/issue/rotate call — there is no way to retrieve them later.

### Self (`/me`, `/me/tokens`) — any authenticated user

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/me` | — | Resolve the calling token to its user (`id`, `email`, `display_name`, `role`). |
| `PATCH` | `/me` | `{display_name?}` | Update your own display name. |
| `GET` | `/me/tokens` | — | List your active and revoked tokens (no raw values). |
| `POST` | `/me/tokens` | `{label?}` | Issue yourself a new token. Raw shown ONCE. |
| `DELETE` | `/me/tokens/{prefix}` | — | Revoke one of your tokens by hash prefix. |

### Admin (`/admin/users`) — caller must be admin

| Method | Path | Body | Purpose |
|---|---|---|---|
| `GET` | `/admin/users` | — | List every account with role, status, token count, last token use. |
| `POST` | `/admin/users` | `{email, display_name?, role?, id?, token_label?}` | Create a user and mint a first token. Raw shown ONCE. |
| `GET` | `/admin/users/{id}` | — | Full user record + thread/todo/platform counts. |
| `PATCH` | `/admin/users/{id}` | `{display_name?, role?, disabled?}` | Update fields. Demoting / disabling the only enabled admin → 409. |
| `DELETE` | `/admin/users/{id}` | — | Cascade delete (tokens, platform identities). 409 if user owns threads or todos — clean those up first. |
| `GET` | `/admin/users/{id}/tokens` | — | List a user's tokens (no raw). |
| `POST` | `/admin/users/{id}/tokens` | `{label?}` | Issue a token for the user. Raw shown ONCE. |
| `POST` | `/admin/users/{id}/tokens/rotate` | `{label?}` | Revoke every active token for the user, mint a fresh one. Raw shown ONCE. |
| `DELETE` | `/admin/users/{id}/tokens/{prefix}` | — | Revoke a single token by hash prefix. |
| `GET` | `/admin/users/{id}/platforms` | — | List a user's linked Discord/Telegram/Twitch identities. |
| `POST` | `/admin/users/{id}/platforms` | `{provider, provider_user_id}` | Link a platform identity. 409 if already linked to another user. |
| `DELETE` | `/admin/users/{id}/platforms/{provider}/{provider_user_id}` | — | Unlink. |

The `prefix` in token revoke paths is the first 8 hex chars of the token's sha256 (returned in the `token_hash_prefix` field of `GET /me/tokens` and the admin token list). Prefixes shorter than 4 chars are rejected; ambiguous prefixes return 400.

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
docker exec nymeria-api python run.py users add bot-service@localhost --role admin --id bot-service
```

Copy the printed token into `.env.docker`:

```
NYMERIA_SERVICE_TOKEN=nym_...
```

Then `docker compose --env-file .env.docker up -d` to propagate the variable into every container. Discord/Telegram/Watchdog print `Auth: service token` at startup when they pick it up — `NYMERIA_API_KEY` was retired in Step 3c, so the service token is now the only way for shared infrastructure to authenticate.

**How the header is honored:** Every authenticated route now resolves the caller via `verify_api_key`/`require_user`, which honors `X-Nymeria-Act-As: <user_id>` for admin callers. Non-admin callers sending it get 403; unknown/disabled targets get 404. Bots and the watchdog rely on this everywhere — they hold the admin service token and act-as the resolved per-user identity per request.

**What the bots send:**
```
Authorization: Bearer <NYMERIA_SERVICE_TOKEN>
X-Nymeria-Act-As: <resolved_user_id>
```
where `<resolved_user_id>` is the Nymeria account the platform user maps to via `platform_identities` (Step 6).

## Storage location

- **Local:** `Nymeria/data/accounts.db` (override with `NYMERIA_DATA_DIR`)
- **Docker:** `nymeria_data:/data/accounts.db` (the `nymeria_data` named volume)

Back this file up alongside `nymeria.db` — losing it locks every user out.

## Per-user OAuth token caches

Google (Calendar/Docs/Drive/Sheets) and Microsoft (Outlook/Graph) OAuth tokens live at `data/auth_tokens/<user_id>/` — isolated per Nymeria user so one account's bot agent can't read another's Gmail/Outlook.

| Service | Cache file |
|---|---|
| Google Calendar | `data/auth_tokens/<user_id>/google_calendar.json` |
| Google Docs/Drive/Sheets | `data/auth_tokens/<user_id>/google_docs.json` |
| Microsoft Outlook/Graph | `data/auth_tokens/<user_id>/microsoft.json` |

**On first boot with the per-user refactor**, existing global caches (`data/auth_tokens/.microsoft_mcp_token_cache.json` etc.) are automatically migrated to `data/auth_tokens/default/*.json`. The owner's existing Google/Outlook auth survives — other users start with empty caches and run the usual `calendar_auth_start` / `google_docs_auth_start` / `outlook_auth_start` tool flows to authenticate their own accounts independently.

Tools resolve the current caller's `user_id` via `RunnableConfig` injection (the agent sets `configurable.user_id` on every graph invocation). No tool can be tricked into loading a different user's token cache.

## Rationale for dedicated SQLite

Accounts live in their own file rather than co-located with LangGraph's checkpoint database. The checkpoint backend is backend-switchable (SQLite locally, Postgres in Docker); keeping accounts SQLite-only keeps the account-layer code single-backend and avoids forcing a `psycopg` dependency path for what is fundamentally a low-volume, latency-insensitive store. The only cross-DB operation (Step 5's legacy thread backfill) reads `DISTINCT thread_id FROM checkpoints` and writes to `thread_owners` — two connections, one-shot, trivial.
