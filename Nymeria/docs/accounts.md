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

## CLI

All account ops use `python run.py users <action>`. The CLI operates directly on the accounts DB — no running API required, so you can provision accounts even when the API is down.

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

## Service token

Bots, the ticker, and the watchdog call the API as admin with `X-Nymeria-Act-As: <user_id>` to route per-user traffic without holding each user's raw token. Create the service user once:

```bash
docker exec nymeria-api python run.py users add bot-service@localhost --role admin --id bot-service
```

Copy the printed token into `.env.docker` as `NYMERIA_SERVICE_TOKEN=nym_...` and restart the relevant containers. The setting is already wired into `config/settings.py` (`nymeria_service_token`) — middleware and bot consumption land in Step 3/Step 6.

## Storage location

- **Local:** `Nymeria/data/accounts.db` (override with `NYMERIA_DATA_DIR`)
- **Docker:** `nymeria_data:/data/accounts.db` (the `nymeria_data` named volume)

Back this file up alongside `nymeria.db` — losing it locks every user out.

## Rationale for dedicated SQLite

Accounts live in their own file rather than co-located with LangGraph's checkpoint database. The checkpoint backend is backend-switchable (SQLite locally, Postgres in Docker); keeping accounts SQLite-only keeps the account-layer code single-backend and avoids forcing a `psycopg` dependency path for what is fundamentally a low-volume, latency-insensitive store. The only cross-DB operation (Step 5's legacy thread backfill) reads `DISTINCT thread_id FROM checkpoints` and writes to `thread_owners` — two connections, one-shot, trivial.
