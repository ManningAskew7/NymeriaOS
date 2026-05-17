# WhatsApp Bot

Nymeria's WhatsApp integration is hosted by the API service as a WhatsApp Business Cloud API webhook client. It receives Meta webhook payloads, resolves WhatsApp senders to Nymeria users, streams chat turns through the in-process agent, and sends replies through the Graph API.

This client uses the official Cloud API. It does not use WhatsApp Web, QR login, Baileys, or a personal-account bridge.

## Architecture

```text
Meta WhatsApp Cloud webhook
     -> Nymeria API /integrations/whatsapp/webhook
          -> NymeriaWhatsAppBot
               -> platform identity and chat binding repos
               -> NymeriaAgent.astream()
               -> WhatsApp Graph /<phone-number-id>/messages
```

There is no separate `run.py whatsapp-bot` process. The API container owns the webhook route, so the public API URL must be reachable from Meta over HTTPS.

## Setup

1. Create or select a Meta app with WhatsApp Business Platform enabled.
2. Add a WhatsApp sender phone number and obtain its Phone Number ID.
3. Create a permanent access token with WhatsApp messaging permissions.
4. Configure the webhook callback URL:

```text
https://your-api-host.example.com/integrations/whatsapp/webhook
```

5. Subscribe the app to WhatsApp `messages` webhooks.
6. Set the environment variables below and restart the API container.
7. Link WhatsApp senders to Nymeria users before sending normal chat traffic.

## Environment

Add to `.env.docker`:

```bash
WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_BASE_URL=https://graph.facebook.com/v19.0
WHATSAPP_WEBHOOK_VERIFY_TOKEN=choose-a-long-random-token
WHATSAPP_APP_SECRET=...          # recommended; verifies X-Hub-Signature-256
WHATSAPP_SHOW_TOOL_EVENTS=false
```

`WHATSAPP_WEBHOOK_VERIFY_TOKEN` is the token you type into Meta's webhook setup UI. Nymeria returns the `hub.challenge` only when Meta sends the same token.

`WHATSAPP_APP_SECRET` is optional but recommended. When set, POST webhooks must include a valid `X-Hub-Signature-256` HMAC.

## Linking Users

WhatsApp senders must be linked to Nymeria accounts before agent traffic is accepted. Nymeria stores WhatsApp identities as the Cloud API sender `wa_id`/`from` value normalized to digits.

Example admin link:

```bash
python3 run.py users link-platform user@example.com whatsapp 15551234567
```

Self-service link codes also work. Issue a platform link code for provider `whatsapp`, then send:

```text
link <code>
```

The bot also accepts `link_<code>`.

## Thread Binding

WhatsApp chats can be bound to desktop-created Nymeria threads with chat-app bind codes:

```text
bind <code>
unbind
```

The binding key for a direct chat is:

```text
whatsapp:<sender_id>
```

## Native Thread IDs

| WhatsApp context | Nymeria thread ID |
|------------------|-------------------|
| Direct Cloud API chat | `whatsapp_<sender_id>` |

WhatsApp direct-chat threads are private per sender. Future group-style IDs should use `whatsapp_group_<id>` and are classified as shared.

## Commands

Plain text commands handled by the WhatsApp bot:

| Command | Description |
|---------|-------------|
| `link <code>` | Link the WhatsApp sender to a Nymeria account. |
| `bind <code>` | Bind the current WhatsApp chat to a Nymeria thread. |
| `unbind` | Remove the current WhatsApp chat binding. |
| `stop` | Abort the current Nymeria run for this WhatsApp chat. |

Other text, button, and interactive-reply messages are sent to Nymeria as chat turns.

## Limitations

- Cloud API only: no personal WhatsApp Web session or QR login.
- Text, button, and interactive reply messages only in v1; inbound media is ignored.
- Meta/WhatsApp conversation-window and template-message rules still apply. Nymeria replies to inbound user messages, but proactive outreach may require approved templates.
