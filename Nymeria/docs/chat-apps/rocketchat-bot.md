# Rocket.Chat Bot

Nymeria's Rocket.Chat integration runs as a thin realtime client. It receives Rocket.Chat room messages through the realtime room stream, resolves Rocket.Chat users to Nymeria accounts, streams chat turns through the Nymeria REST API, and sends replies back through the Rocket.Chat REST API. No agent instance runs inside the Rocket.Chat bot container.

## Architecture

```text
Docker: nymeria-rocketchat-bot (profile: rocketchat)
     -> NymeriaRocketChatBot
          -> Rocket.Chat REST /api/v1/me, /api/v1/subscriptions.get, /api/v1/chat.postMessage
          -> Rocket.Chat realtime stream-room-messages
          -> NymeriaAPIClient (async httpx -> Nymeria REST API)
          -> SSE chat stream -> Rocket.Chat replies
```

Rocket.Chat uses one bot/user token pair. The same `ROCKETCHAT_AUTH_TOKEN`, `ROCKETCHAT_USER_ID`, and `ROCKETCHAT_BASE_URL` settings are also used by Nymeria's native Rocket.Chat tools.

## Setup

1. Create a Rocket.Chat bot user or dedicated service user.
2. Create a personal access token for that user.
3. Invite the bot user to rooms it should read and post in.
4. Configure `.env.docker`.
5. Link Rocket.Chat users to Nymeria accounts before sending normal chat traffic.
6. Enable the Docker Compose `rocketchat` profile.

## Environment

Add to `.env.docker`:

```bash
ROCKETCHAT_BASE_URL=https://chat.example.com
ROCKETCHAT_USER_ID=...
ROCKETCHAT_AUTH_TOKEN=...
ROCKETCHAT_RESPOND_MODE=mention        # mention (default) or all
ROCKETCHAT_SHOW_TOOL_EVENTS=false      # true posts compact tool call/result messages
```

`ROCKETCHAT_RESPOND_MODE=mention` means:

- DMs always respond.
- Channels and group conversations respond to `@<bot_username>` mentions.
- Thread replies continue when the bot is already participating in that Rocket.Chat thread.

`ROCKETCHAT_RESPOND_MODE=all` makes rooms respond to every non-bot message the bot receives. Use this only for controlled rooms.

## Start

```bash
docker compose --profile rocketchat --env-file .env.docker up -d --build
docker logs nymeria-rocketchat-bot --tail 50
```

Restart after code or environment changes:

```bash
docker compose --env-file .env.docker restart rocketchat-bot
```

Local thin-client run:

```bash
python3 run.py rocketchat-bot --api-url http://localhost:8000
```

## Linking Users

Rocket.Chat users must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores Rocket.Chat identities as:

```text
<server_host>:<rocketchat_user_id>
```

Example admin link:

```bash
python3 run.py users link-platform user@example.com rocketchat chat.example.com:abc123
```

Self-service link codes also work. Issue a platform link code for provider `rocketchat`, then DM the bot or mention it with:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

Rocket.Chat conversations can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Bindings are scoped to the current room/thread. The bot checks thread-specific bindings first, then room-level bindings, then falls back to native Rocket.Chat thread IDs.

## Native Thread IDs

| Rocket.Chat context | Nymeria thread ID |
|---------------------|-------------------|
| DM | `rocketchat_dm_<server_host>_<rocketchat_user_id>` |
| Room/group | `rocketchat_<server_host>_<room_id>` |
| Rocket.Chat thread | `rocketchat_<server_host>_<room_id>_thread_<root_message_id>` |

Rocket.Chat room threads are treated as shared channels in Nymeria access control. DMs are per-user.

## Commands

Plain text commands handled by the Rocket.Chat bot:

| Command | Description |
|---------|-------------|
| `link <code>` | Link the Rocket.Chat user to a Nymeria account. |
| `bind <code>` | Bind the current Rocket.Chat conversation to a Nymeria thread. |
| `unbind` | Remove the current Rocket.Chat conversation binding. |
| `stop` | Abort the current Nymeria run for this Rocket.Chat conversation. |

Other accepted messages are sent to Nymeria as chat turns.

## Notes

- The bot uses Rocket.Chat's REST API for identity, room subscription discovery, and posting replies.
- The bot uses the realtime `stream-room-messages` stream for inbound messages.
- Duplicate messages are deduped by message ID.
- Replies are chunked below 4000 characters and posted into Rocket.Chat threads when available.
