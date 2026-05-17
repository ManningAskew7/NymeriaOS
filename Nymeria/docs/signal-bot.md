# Signal Bot

Nymeria's Signal integration runs as a thin client against a user-managed `signal-cli-rest-api` daemon. Signal does not provide a hosted first-party bot API, so Nymeria receives `/api/v1/events` SSE records from the daemon, sends replies through JSON-RPC `send`, and routes accepted text messages through the Nymeria REST/SSE chat API.

## Architecture

```text
Docker: nymeria-signal-bot (profile: signal)
     -> NymeriaSignalBot
          -> SignalCliRestClient (/api/v1/check, /api/v1/events, /api/v1/rpc)
          -> NymeriaAPIClient (async httpx -> Nymeria REST API)
          -> Signal receive events -> SSE chat stream -> Signal messages
```

The Signal account and `signal-cli-rest-api` data are outside Nymeria. Keep the daemon running in `json-rpc` or `json-rpc-native` mode so `/api/v1/events?account=<phone>` and `/api/v1/rpc` are available.

## Setup

1. Register or link a Signal account with `signal-cli`.
2. Run `signal-cli-rest-api` in JSON-RPC mode and confirm `GET /api/v1/check` returns healthy.
3. Set `SIGNAL_HTTP_URL` and `SIGNAL_ACCOUNT` in `.env.docker`.
4. Link Signal senders to Nymeria accounts.
5. Enable the Docker Compose `signal` profile.

## Environment

```bash
SIGNAL_HTTP_URL=http://host.docker.internal:8080
SIGNAL_ACCOUNT=+15551234567
SIGNAL_ACCOUNT_UUID=
SIGNAL_RESPOND_MODE=mention
SIGNAL_ALLOWED_USERS=
SIGNAL_ALLOWED_GROUPS=
SIGNAL_SHOW_TOOL_EVENTS=false
SIGNAL_HTTP_TIMEOUT=30.0
```

`SIGNAL_HTTP_URL` is the base URL for `signal-cli-rest-api`. If the daemon runs on the Docker host, `http://host.docker.internal:8080` works with the compose service. If it runs in another container on the same Docker network, use that container's service name.

`SIGNAL_RESPOND_MODE=mention` means:

- Direct messages respond when the sender is linked or is running a `link` command.
- Group messages require a Signal mention of the bot account.
- `SIGNAL_ALLOWED_USERS` and `SIGNAL_ALLOWED_GROUPS` restrict processing before Nymeria user resolution.

`SIGNAL_RESPOND_MODE=all` responds to every allowed group text message the bot receives. Use it only for controlled groups.

## Start

```bash
docker compose --profile signal --env-file .env.docker up -d --build signal-bot
docker logs nymeria-signal-bot --tail 50
```

Restart after code or environment changes:

```bash
docker compose --env-file .env.docker restart signal-bot
```

Local thin-client run:

```bash
python3 run.py signal-bot --api-url http://localhost:8000
```

## Linking Users

Signal users must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores Signal identities as the normalized sender phone number when `sourceNumber` is present, otherwise as `uuid:<sourceUuid>`.

Example admin link:

```bash
python3 run.py users link-platform user@example.com signal +15550001111
```

Self-service link codes also work. Issue a platform link code for provider `signal`, then send:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

Signal conversations can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Bindings are scoped to the current Signal conversation:

```text
signal:dm:<sender_id>
signal:group:<group_id>
```

## Native Thread IDs

| Signal context | Nymeria thread ID |
|---|---|
| Direct message | `signal_dm_<sender_id>` |
| Group chat | `signal_group_<group_id>` |

Signal groups are treated as shared channels in Nymeria access control. Direct messages are per-user.

## Commands

Plain text commands handled by the Signal bot:

| Command | Description |
|---|---|
| `link <code>` | Link the Signal sender to a Nymeria account. |
| `bind <code>` | Bind the current Signal conversation to a Nymeria thread. |
| `unbind` | Remove the current Signal chat binding. |
| `stop` | Abort the current Nymeria run for this Signal chat. |

Slash-style `/stop` is also accepted.

## Limits And Security

- This is an unofficial Signal integration through `signal-cli`; monitor upstream compatibility before production use.
- Nymeria ignores sync messages, self echoes, read receipts, and contentless metadata events to avoid reply loops.
- Nymeria sends text-only replies in v1 and splits outgoing messages at 8000 characters.
- Group operation defaults to mention-gated behavior. Use allowlists and `SIGNAL_RESPOND_MODE=all` only in trusted groups.
- The Signal account credentials and message store belong to `signal-cli-rest-api`; secure that daemon and its volume separately from Nymeria.

## References

- `signal-cli`: https://github.com/AsamK/signal-cli
- `signal-cli` JSON-RPC usage: https://packaging.gitlab.io/signal-cli/usage/
- `signal-cli-rest-api`: https://github.com/bbernhard/signal-cli-rest-api
- `signal-cli-rest-api` API reference: https://deepwiki.com/bbernhard/signal-cli-rest-api/4-api-reference
