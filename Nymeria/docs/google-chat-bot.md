# Google Chat Bot

Nymeria's Google Chat integration is an API-hosted HTTPS webhook. It receives Google Chat `MESSAGE` interaction events at `/integrations/google-chat/webhook`, resolves the sender to a Nymeria account, streams the turn through the in-process agent, and replies through the Google Chat REST API.

There is no standalone `run.py google-chat-bot` command and no Docker Compose bot service. The API service hosts the webhook.

## Setup

1. Enable the Google Chat API in the Google Cloud project that owns the Chat app.
2. Configure the Chat app's interaction endpoint:

   ```text
   https://<your-public-api-host>/integrations/google-chat/webhook
   ```

3. Configure Google Chat request authentication. The default Nymeria mode expects the endpoint URL as the token audience. If your app uses project-number audience, set `GOOGLE_CHAT_AUTH_AUDIENCE_TYPE=project-number` and `GOOGLE_CHAT_PROJECT_NUMBER`.
4. Provide service-account credentials for async replies. Prefer `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` in production, or `GOOGLE_CHAT_SERVICE_ACCOUNT_JSON` for JSON content/path in controlled environments.
5. Link Google Chat users to Nymeria accounts before sending normal chat traffic.

## Environment

```bash
GOOGLE_CHAT_SERVICE_ACCOUNT_FILE=/run/secrets/google-chat-service-account.json
GOOGLE_CHAT_SERVICE_ACCOUNT_JSON=
GOOGLE_CHAT_USE_ADC=false
GOOGLE_CHAT_PROJECT_NUMBER=123456789012
GOOGLE_CHAT_AUTH_AUDIENCE_TYPE=app-url
GOOGLE_CHAT_AUTH_AUDIENCE=
GOOGLE_CHAT_BOT_NAME=Nymeria
GOOGLE_CHAT_RESPOND_MODE=mention
GOOGLE_CHAT_VALIDATE_AUTH=true
GOOGLE_CHAT_SHOW_TOOL_EVENTS=false
GOOGLE_CHAT_API_BASE_URL=https://chat.googleapis.com/v1
```

`GOOGLE_CHAT_RESPOND_MODE=mention` means:

- Direct messages always respond.
- Spaces and group chats respond to app mentions.
- A thread continues accepting follow-up replies after Nymeria has joined that thread.

`GOOGLE_CHAT_RESPOND_MODE=all` makes spaces/group chats respond to every non-bot message the webhook receives. Use this only in controlled chats.

## Account Linking

Google Chat users must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores Google Chat identities as:

```text
users/<google-chat-user-id>
```

If the user resource is unavailable, Nymeria falls back to `email:<lowercase-email>`.

Admin link example:

```bash
python3 run.py users link-platform user@example.com googlechat users/123456789
```

Self-service link codes also work. Issue a platform link code for provider `googlechat`, then DM the app or mention it with:

```text
link <code>
```

## Thread Binding

Google Chat conversations can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

Bindings are scoped to the current space/thread. The bot checks thread-specific bindings first, then space-level bindings, then falls back to native Google Chat thread IDs.

Native thread IDs:

| Google Chat context | Nymeria thread ID |
|---|---|
| Direct message | `googlechat_dm_<user>` |
| Space/group chat | `googlechat_<space>` |
| Space thread | `googlechat_<space>_thread_<thread>` |

Spaces and group chats are treated as shared channels in Nymeria access control. Direct messages are per-user.

## Commands

Plain text commands handled by the Google Chat bot:

| Command | Behavior |
|---|---|
| `link <code>` | Link the Google Chat user to a Nymeria account. |
| `bind <code>` | Bind the current Google Chat conversation to a Nymeria thread. |
| `unbind` | Remove the current Google Chat conversation binding. |
| `stop` | Abort the current Nymeria run for this Google Chat conversation. |

Slash-style `/stop` is also accepted when Google Chat sends it as message text.

## Limits And Security

- Incoming bearer tokens are validated when `GOOGLE_CHAT_VALIDATE_AUTH=true`.
- Google Chat may retry webhook delivery; Nymeria deduplicates recent event IDs.
- Bot/self messages are ignored.
- Replies use Google Chat app authentication with scope `https://www.googleapis.com/auth/chat.bot`.
- Nymeria splits outgoing Google Chat replies at 4000 characters for readability, below Google Chat's documented message-size ceiling.
