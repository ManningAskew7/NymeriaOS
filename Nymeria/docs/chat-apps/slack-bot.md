# Slack Bot

Nymeria's Slack integration runs as a stateless Socket Mode client. It receives Slack Events API payloads, resolves Slack users to Nymeria accounts, and streams chat turns through the Nymeria REST API. No agent instance runs inside the Slack bot container.

## Architecture

```
Docker: nymeria-slack-bot (profile: slack)
     └─ NymeriaSlackBot (Slack Bolt AsyncApp + Socket Mode)
          ├─ NymeriaAPIClient (async httpx -> Nymeria REST API)
          ├─ Events: message + app_mention
          └─ SSE chat stream -> Slack thread replies
```

Slack uses two credentials:

- `SLACK_BOT_TOKEN` (`xoxb-...`) for Web API calls such as `auth.test` and `chat.postMessage`.
- `SLACK_APP_TOKEN` (`xapp-...`) for Socket Mode. It must have `connections:write`.

## Setup

1. Create a Slack app at https://api.slack.com/apps.
2. Enable **Socket Mode**.
3. Generate an app-level token with `connections:write`.
4. Add a bot user and install the app to the workspace.
5. Subscribe to bot events:
   - `app_mention`
   - `message.im`
   - `message.channels`
   - Optional: `message.groups`, `message.mpim`
6. Add bot scopes:
   - Required: `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`
   - Public channels: `channels:history`, `channels:read`
   - Private channels: `groups:history`, `groups:read`
   - Group DMs: `mpim:history`, `mpim:read`, `mpim:write`
   - Attachments: `files:read`
7. Reinstall the app after changing scopes/events.

## Environment

Add to `.env.docker`:

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_RESPOND_MODE=mention       # mention (default) or all
SLACK_SHOW_TOOL_EVENTS=false     # true posts compact tool call/result messages
```

`SLACK_RESPOND_MODE=mention` means:

- DMs always respond.
- Channels respond to `@Nymeria` app mentions.
- Thread replies continue when the bot is already participating in that Slack thread.

`SLACK_RESPOND_MODE=all` makes channels respond to every non-bot message the app receives. Use this only for controlled channels.

## Start

```bash
docker compose --profile slack --env-file .env.docker up -d --build
docker logs nymeria-slack-bot --tail 50
```

Restart after code or environment changes:

```bash
docker compose --env-file .env.docker restart slack-bot
```

Local thin-client run:

```bash
python3 run.py slack-bot --api-url http://localhost:8000
```

## Linking Users

Slack users must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores Slack identities as:

```text
<team_id>:<slack_user_id>
```

Example admin link:

```bash
python3 run.py users link-platform user@example.com slack T123456:U123456
```

Self-service link codes also work. Issue a platform link code for provider `slack`, then DM the bot or mention it with:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

Slack conversations can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Bindings are scoped to the current Slack channel/thread. The bot checks thread-specific bindings first, then channel-level bindings, then falls back to native Slack thread IDs.

## Native Thread IDs

| Slack context | Nymeria thread ID |
|---------------|-------------------|
| DM | `slack_dm_<team_id>_<slack_user_id>` |
| Channel | `slack_<team_id>_<channel_id>` |
| Slack thread | `slack_<team_id>_<channel_id>_thread_<thread_ts>` |

Slack channel threads are treated as shared channels in Nymeria access control. DMs are per-user.

## Commands

Plain text commands handled by the Slack bot:

| Command | Description |
|---------|-------------|
| `link <code>` | Link the Slack user to a Nymeria account. |
| `bind <code>` | Bind the current Slack conversation to a Nymeria thread. |
| `unbind` | Remove the current Slack conversation binding. |
| `stop` | Abort the current Nymeria run for this Slack conversation. |

Any other message starting with `/` is forwarded verbatim to the backend
slash-command registry (the same catalog desktop, mobile, the CLI, Telegram,
and Discord use) and the command's markdown result is posted back in chat, so
`/status`, `/todos list`, `/hook log`, and every future registered command
work without bot changes. Per-surface menus and admin gating apply
(`surface="slack"`). Commands that execute as chat turns (`/skill`, `/kit`)
fall through to the normal chat path automatically. The local commands above
are matched first (with or without a leading `/`).

Slack itself intercepts messages that start with `/` as Slack-native slash
commands, and unregistered ones never reach the bot. Use `!command` instead
(`!status`, `!todos list`), which the bot normalizes to `/command` before
forwarding; in channels, the mention form (`@Nymeria /status`) also arrives
intact because the `/` is not at the start of the raw message. Any message
starting with `!` immediately followed by a letter is treated as a command
attempt, so chat that begins that way (`!important ...`) returns an unknown
command error; reword it or drop the leading `!`. Because a leading `/` is
untypeable on Slack, command results rewrite every backtick-quoted command
reference (`/help`, a suggested `/provider`, usage strings) to the `!`
prefix before posting; backticked file paths keep their slash.
Chat-stream commands that fall through (`!skill`,
`!quick`, ...) reach the chat route as their normalized `/` form with no
channel-context prefix, so they are detected correctly in channels too
(fixed 2026-08-02).

Other messages are sent to Nymeria as chat turns.

## Notes

- Socket Mode avoids exposing a public Slack webhook URL, which matches self-hosted deployments.
- The bot handles both `message` and `app_mention` events because Slack workspaces can deliver mentions through either path.
- Duplicate Slack events are deduped by channel and timestamp.
- Slack posting is chunked below message limits and sent into Slack threads when available.
