# Matrix Bot

Nymeria's Matrix integration runs as a thin Client-Server API sync client. It long-polls `/sync`, converts Matrix room messages into Nymeria REST/SSE chat calls, and sends replies back with `m.room.message`.

This first Matrix client supports plaintext Matrix rooms. Encrypted `m.room.encrypted` events are detected and answered once per room with a setup notice; E2EE support can be added later with a Matrix SDK and key store.

## Architecture

```
Docker: nymeria-matrix-bot (profile: matrix)
     └─ NymeriaMatrixBot
          ├─ MatrixHTTPClient (/_matrix/client/v3)
          ├─ NymeriaAPIClient (async httpx -> Nymeria REST API)
          └─ /sync events -> SSE chat stream -> Matrix replies
```

The bot performs an initial baseline sync and stores the returned `next_batch` token in memory before processing new events. This avoids answering historical room messages after startup.

## Setup

1. Create a Matrix account for the bot on your homeserver.
2. Invite the bot account to rooms you want it to serve.
3. Configure token or password auth in `.env.docker`.
4. Link Matrix users to Nymeria accounts.
5. Enable the Docker Compose `matrix` profile.

## Environment

Token auth:

```bash
MATRIX_HOMESERVER=https://matrix.example.org
MATRIX_ACCESS_TOKEN=syt_...
MATRIX_USER_ID=@nymeria:example.org     # optional with token; resolved via /account/whoami if omitted
MATRIX_RESPOND_MODE=mention             # mention (default) or all
MATRIX_FREE_RESPONSE_ROOMS=             # comma-separated room IDs that do not need bot mentions
MATRIX_AUTO_JOIN=false                  # true auto-joins all room invites
```

Password auth:

```bash
MATRIX_HOMESERVER=https://matrix.example.org
MATRIX_USER_ID=@nymeria:example.org
MATRIX_PASSWORD=...
MATRIX_DEVICE_ID=NYMERIA_BOT            # optional stable device ID
```

`MATRIX_RESPOND_MODE=mention` means:

- Low-member DM-like rooms respond without mention when `/sync` room summaries indicate two or fewer members.
- Rooms listed in `MATRIX_FREE_RESPONSE_ROOMS` respond without mention.
- Other rooms require a mention of the full bot Matrix user ID, or an `m.mentions.user_ids` entry.

`MATRIX_RESPOND_MODE=all` responds to every plaintext `m.text` or `m.notice` message the bot receives. Use it only for controlled rooms.

`MATRIX_AUTO_JOIN=false` is the safe default. If set to `true`, the bot joins every invited room.

## Start

```bash
docker compose --profile matrix --env-file .env.docker up -d --build
docker logs nymeria-matrix-bot --tail 50
```

Restart after code or environment changes:

```bash
docker compose --env-file .env.docker restart matrix-bot
```

Local thin-client run:

```bash
python3 run.py matrix-bot --api-url http://localhost:8000
```

## Linking Users

Matrix users must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores Matrix identities as the full Matrix user ID:

```text
@alice:example.org
```

Example admin link:

```bash
python3 run.py users link-platform user@example.com matrix @alice:example.org
```

Self-service link codes also work. Issue a platform link code for provider `matrix`, then send:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

Matrix rooms can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Bindings are scoped to the current room, or to a Matrix thread root event when the inbound message includes an `m.thread` relation.

## Native Thread IDs

| Matrix context | Nymeria thread ID |
|----------------|-------------------|
| Room | `matrix_<room_id_sanitized>` |
| Matrix thread relation | `matrix_<room_id_sanitized>_thread_<event_id_sanitized>` |

Matrix rooms are treated as shared channels in Nymeria access control. The bot still resolves and enforces the linked Nymeria user before sending messages to the agent.

## Commands

Plain text commands handled by the Matrix bot:

| Command | Description |
|---------|-------------|
| `link <code>` | Link the Matrix user to a Nymeria account. |
| `bind <code>` | Bind the current Matrix room/thread to a Nymeria thread. |
| `unbind` | Remove the current Matrix binding. |
| `stop` | Abort the current Nymeria run for this Matrix room/thread. |

Other accepted messages are sent to Nymeria as chat turns.

## Limitations

- Plaintext rooms only in v1.
- Text messages only; media attachments are not downloaded yet.
- Auto-join has only `false`/`true` behavior. Use `false` unless the bot account is deployed in a trusted homeserver/workspace.
