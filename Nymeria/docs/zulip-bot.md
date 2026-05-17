# Zulip Bot

Nymeria's Zulip integration runs as a thin Events API client. It registers a Zulip event queue, long-polls message events, resolves Zulip senders to Nymeria accounts, streams chat turns through the Nymeria REST API, and sends replies back through the Zulip Messages API. No agent instance runs inside the Zulip bot container.

## Architecture

```text
Docker: nymeria-zulip-bot (profile: zulip)
     -> NymeriaZulipBot
          -> Zulip POST /api/v1/register
          -> Zulip GET /api/v1/events
          -> Zulip POST /api/v1/messages
          -> NymeriaAPIClient (async httpx -> Nymeria REST API)
          -> SSE chat stream -> Zulip replies
```

Zulip uses one bot identity: the bot email plus API key. The same `ZULIP_API_KEY`, `ZULIP_EMAIL`, and `ZULIP_BASE_URL` settings are also used by Nymeria's native Zulip tools.

## Setup

1. Create a Zulip bot in Personal settings -> Bots.
2. Copy the bot email and API key.
3. Subscribe the bot to channels it should read and post in.
4. Configure `.env.docker`.
5. Link Zulip users to Nymeria accounts before sending normal chat traffic.
6. Enable the Docker Compose `zulip` profile.

## Environment

Add to `.env.docker`:

```bash
ZULIP_BASE_URL=https://your-org.zulipchat.com
ZULIP_EMAIL=nymeria-bot@your-org.zulipchat.com
ZULIP_API_KEY=...
ZULIP_RESPOND_MODE=mention        # mention (default) or all
ZULIP_SHOW_TOOL_EVENTS=false      # true posts compact tool call/result messages
```

`ZULIP_RESPOND_MODE=mention` means:

- Direct messages always respond.
- Channels respond when Zulip marks the message as `mentioned`, or when the message contains the bot's full-name mention.

`ZULIP_RESPOND_MODE=all` makes channels respond to every non-bot message in subscribed channels. Use this only for controlled channels.

## Start

```bash
docker compose --profile zulip --env-file .env.docker up -d --build
docker logs nymeria-zulip-bot --tail 50
```

Restart after code or environment changes:

```bash
docker compose --env-file .env.docker restart zulip-bot
```

Local thin-client run:

```bash
python3 run.py zulip-bot --api-url http://localhost:8000
```

## Linking Users

Zulip users must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores Zulip identities as:

```text
<realm_host>:<zulip_user_id>
```

Example admin link:

```bash
python3 run.py users link-platform user@example.com zulip your-org.zulipchat.com:42
```

Self-service link codes also work. Issue a platform link code for provider `zulip`, then DM the bot or mention it with:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

Zulip conversations can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Binding keys:

```text
<realm_host>:direct:<sender_id>
<realm_host>:stream:<stream_id>
<realm_host>:stream:<stream_id>:<topic>
```

The bot checks topic-specific stream bindings first, then stream-level bindings, then falls back to native Zulip thread IDs.

## Native Thread IDs

| Zulip context | Nymeria thread ID |
|---------------|-------------------|
| Direct message | `zulip_dm_<realm_host>_<sender_id>` |
| Stream | `zulip_<realm_host>_<stream_id>` |
| Stream topic | `zulip_<realm_host>_<stream_id>_topic_<topic>` |

Zulip stream threads are treated as shared channels in Nymeria access control. Direct-message threads are per-user.

## Commands

Plain text commands handled by the Zulip bot:

| Command | Description |
|---------|-------------|
| `link <code>` | Link the Zulip sender to a Nymeria account. |
| `bind <code>` | Bind the current Zulip direct message or stream topic to a Nymeria thread. |
| `unbind` | Remove the current Zulip chat binding. |
| `stop` | Abort the current Nymeria run for this Zulip conversation. |

Other accepted messages are sent to Nymeria as chat turns.

## Notes

- The bot registers `event_types=["message"]` and `apply_markdown=false`.
- Zulip event queues can expire; the bot re-registers when Zulip returns `BAD_EVENT_QUEUE_ID`.
- Duplicate events are deduped by event ID.
- Replies are chunked below 4000 characters.
