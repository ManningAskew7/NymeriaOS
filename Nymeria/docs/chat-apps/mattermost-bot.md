# Mattermost Bot

Nymeria's Mattermost integration runs as a thin WebSocket client. It receives Mattermost `posted` events, resolves Mattermost users to Nymeria accounts, streams chat turns through the Nymeria REST API, and sends replies back through the Mattermost REST API. No agent instance runs inside the Mattermost bot container.

## Architecture

```text
Docker: nymeria-mattermost-bot (profile: mattermost)
     -> NymeriaMattermostBot
          -> Mattermost WebSocket /api/v4/websocket
          -> Mattermost REST API /api/v4/users/me and /api/v4/posts
          -> NymeriaAPIClient (async httpx -> Nymeria REST API)
          -> SSE chat stream -> Mattermost thread replies
```

Mattermost uses one bot account token. The same `MATTERMOST_ACCESS_TOKEN` and `MATTERMOST_BASE_URL` settings are also used by Nymeria's native Mattermost tools.

## Setup

1. Create a Mattermost bot account in System Console -> Integrations -> Bot Accounts.
2. Copy the generated bot access token before closing the setup page.
3. Invite the bot account to teams and channels it should read and post in.
4. Configure `.env.docker`.
5. Link Mattermost users to Nymeria accounts before sending normal chat traffic.
6. Enable the Docker Compose `mattermost` profile.

## Environment

Add to `.env.docker`:

```bash
MATTERMOST_BASE_URL=https://mattermost.example.com
MATTERMOST_ACCESS_TOKEN=...
MATTERMOST_RESPOND_MODE=mention        # mention (default) or all
MATTERMOST_SHOW_TOOL_EVENTS=false      # true posts compact tool call/result messages
```

`MATTERMOST_RESPOND_MODE=mention` means:

- DMs always respond.
- Channels and group conversations respond to `@<bot_username>` mentions.
- Thread replies continue when the bot is already participating in that Mattermost thread.

`MATTERMOST_RESPOND_MODE=all` makes channels respond to every non-bot post the bot receives. Use this only for controlled channels.

## Start

```bash
docker compose --profile mattermost --env-file .env.docker up -d --build
docker logs nymeria-mattermost-bot --tail 50
```

Restart after code or environment changes:

```bash
docker compose --env-file .env.docker restart mattermost-bot
```

Local thin-client run:

```bash
python3 run.py mattermost-bot --api-url http://localhost:8000
```

## Linking Users

Mattermost users must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores Mattermost identities as:

```text
<server_host>:<mattermost_user_id>
```

Example admin link:

```bash
python3 run.py users link-platform user@example.com mattermost mattermost.example.com:abc123
```

Self-service link codes also work. Issue a platform link code for provider `mattermost`, then DM the bot or mention it with:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

Mattermost conversations can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Bindings are scoped to the current channel/thread. The bot checks thread-specific bindings first, then channel-level bindings, then falls back to native Mattermost thread IDs.

## Native Thread IDs

| Mattermost context | Nymeria thread ID |
|--------------------|-------------------|
| DM | `mattermost_dm_<server_host>_<mattermost_user_id>` |
| Channel/group | `mattermost_<server_host>_<channel_id>` |
| Mattermost thread | `mattermost_<server_host>_<channel_id>_thread_<root_post_id>` |

Mattermost channel threads are treated as shared channels in Nymeria access control. DMs are per-user.

## Commands

Plain text commands handled by the Mattermost bot:

| Command | Description |
|---------|-------------|
| `link <code>` | Link the Mattermost user to a Nymeria account. |
| `bind <code>` | Bind the current Mattermost conversation to a Nymeria thread. |
| `unbind` | Remove the current Mattermost conversation binding. |
| `stop` | Abort the current Nymeria run for this Mattermost conversation. |

Other accepted messages are sent to Nymeria as chat turns.

## Notes

- The WebSocket URL is derived from `MATTERMOST_BASE_URL` as `/api/v4/websocket`.
- The bot authenticates over the WebSocket with Mattermost's `authentication_challenge` action.
- Mattermost `posted` event `data.post` payloads are parsed whether the post is delivered as a JSON string or an object.
- Duplicate posts are deduped by post ID.
- Replies are chunked below 4000 characters and posted into Mattermost threads when available.
