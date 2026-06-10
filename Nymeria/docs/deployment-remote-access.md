# Remote access

Your Nymeria backend runs on some machine: a VPS, a home server, a Raspberry Pi. You want to talk to it from somewhere else: your laptop at work, your phone on mobile, a teammate's machine.

This page covers four paths, ordered roughly from simplest to most powerful. **You can use more than one at a time**. Telegram plus Tailscale is a common combination.

**The setup wizard automates the Tailscale and Cloudflare paths.** Run `nymeria init external_access`: it detects the tool, drives the login or provisioning, exposes the backend, verifies the resulting URL end to end (including SSE streaming, which some relays silently break), and writes `NYMERIA_PUBLIC_URL` plus the matching `CORS_ORIGINS` entry for you. The manual steps below remain valid and are what the wizard does under the hood.

## TL;DR

| Goal | Use |
|---|---|
| Chat with my assistant from anywhere via Telegram, Discord, Slack, etc. | [Chat-app bots](#chat-app-bots) |
| Access the web/desktop UI from my personal devices, with no public exposure | [Tailscale](#tailscale) |
| Public URL on a free tier, no VPS or custom domain needed | [Cloudflare Tunnel](#cloudflare-tunnel) |
| Production deployment with a custom domain | [Domain + Caddy](#domain--caddy) |

---

## Chat-app bots

**This is the path most people overlook.** If your goal is "talk to my AI from anywhere," you don't need any of the network setup below. You just need a bot token.

Nymeria ships ready-to-use outbound bot integrations for Telegram, Discord, Slack, Matrix, Signal, Mattermost, Zulip, and Rocket.Chat. Each one works by having a bot daemon make outbound connections to the chat platform's API: polling for Telegram, Socket Mode for Slack, Gateway for Discord, sync loop for Matrix, and so on. The chat platform routes messages between you and your bot.

Nymeria also supports API-hosted webhook runtimes for WhatsApp, Messenger, Instagram, Webex, Microsoft Teams, Google Chat, and LINE. Those need one of the public URL options below because the chat platform must POST webhooks to your API.

For those outbound bot daemons, **no inbound network access is needed**. Your backend can sit behind any router, NAT, firewall, or ISP that allows outbound HTTPS. No domain, no port forwarding, no tunnel, no TLS certificate.

### Why this is so good for personal use

- You can talk to your assistant from your phone, your work laptop, or anywhere the chat app is installed.
- Your backend's IP and presence are never exposed to anyone but the chat platform.
- Nymeria still enforces account linking: the Telegram/Discord/etc. sender must resolve to a Nymeria user before the bot will run chat turns.
- It works in the slim shape as a separate thin-client Python process and in the Docker stack as a profiled thin-client container.

### Setup (Telegram example)

1. Open `@BotFather` on Telegram, send `/newbot`, follow the prompts.
2. Save the bot token it gives you.
3. In your `.env` or `.env.docker`, set:
   ```
   TELEGRAM_BOT_TOKEN=<token>
   TELEGRAM_DEFAULT_CHAT_ID=<optional-chat-id-for-notifications>
   ```
4. Start the bot:
   - **Slim shape:** in a separate terminal after `python3 run.py slim` has minted `data/SLIM_SERVICE_TOKEN.txt`:
     ```bash
     NYMERIA_SERVICE_TOKEN="$(cat data/SLIM_SERVICE_TOKEN.txt)" \
       python3 run.py telegram-bot --api-url http://127.0.0.1:8000
     ```
   - **Docker stack:** enable the profiled container after setting `TELEGRAM_BOT_TOKEN` and `NYMERIA_SERVICE_TOKEN`:
     ```bash
     docker compose --env-file .env.docker --profile telegram up -d telegram-bot
     ```
5. Open a chat with your bot in Telegram. Send `/start`.
6. Link your Telegram sender to a Nymeria account with the desktop/mobile Chat App wizard or:
   ```bash
   python3 run.py users link-platform <email> telegram <telegram-user-id>
   ```

For other chat platforms see [telegram-bot.md](chat-apps/telegram-bot.md), [discord-bot.md](chat-apps/discord-bot.md), [slack-bot.md](chat-apps/slack-bot.md), [matrix-bot.md](chat-apps/matrix-bot.md), [signal-bot.md](chat-apps/signal-bot.md), etc.

### When bots alone aren't enough

- You want the full web/desktop UI with live streaming tool output.
- You want to upload large files or images beyond what the chat app allows.
- You want a shareable URL for demos or collaboration.

For those, combine bots with one of the options below.

---

## Tailscale

[Tailscale](https://tailscale.com) is a peer-to-peer VPN built on WireGuard. Install it on each of your devices, they get private IPs like `100.x.x.x`, and they can reach each other directly with end-to-end encryption.

**This is the recommended path for personal use**: backend at home, devices everywhere.

### Setup

On the Nymeria server:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the URL printed to authenticate (uses your Google / GitHub / Microsoft account).

Then expose Nymeria to your tailnet with automatic HTTPS:
```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8000
```

This:
- Binds Nymeria to your tailnet's MagicDNS hostname (e.g. `nymeria.your-tailnet.ts.net`)
- Provisions a TLS certificate automatically after HTTPS is enabled for the tailnet
- Makes it reachable only from devices on your tailnet

On each client device (laptop, phone, etc.):
1. Install Tailscale from https://tailscale.com/download
2. Log in with the same account
3. Open `https://nymeria.your-tailnet.ts.net` in a browser, or point nymeria-desktop at that URL

That's it. Zero public exposure, end-to-end encrypted, no domain required, no port forwarding.

**Funnel (optional, public):** `sudo tailscale funnel --bg 8000` exposes the same URL to the whole internet (the wizard offers this as the "public" choice). That trades away the zero-public-exposure property above: the API is then guarded only by Nymeria's token auth, and Funnel does not preserve public client IPs, so per-client rate limiting cannot distinguish callers. Prefer Serve unless you specifically need app-less public access.

### When Tailscale is the right answer

- You access Nymeria from your own devices and nobody else's
- You don't want to expose anything to the public internet
- You are comfortable managing access through your tailnet users and ACLs

### When it isn't

- You want a URL anyone on the internet can hit
- You have many users not under your control

### Self-hosting the control plane

Tailscale's coordination server is a third-party service. If you want zero third-party involvement, [Headscale](https://github.com/juanfont/headscale) is an open-source self-hostable replacement that speaks the Tailscale protocol.

---

## Cloudflare Tunnel

Cloudflare Tunnel runs a small daemon (`cloudflared`) on your server. It opens an outbound connection to Cloudflare's edge network. Public requests hit Cloudflare, get routed through the tunnel, and arrive at Nymeria. **No port forwarding or public IP is required.** Quick tunnels do not require a domain; stable named tunnels do.

### Setup: quick tunnel (no account, ephemeral URL)

```bash
# Install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cloudflared.deb
sudo dpkg -i cloudflared.deb

# Run a quick tunnel
cloudflared tunnel --url http://localhost:8000
```

Cloudflare prints a URL like `https://random-words-here.trycloudflare.com`. Anyone with that URL can reach your Nymeria API. **The URL changes every restart**. This is fine for one-off sharing, not daily use.

**Warning: quick tunnels cannot carry Nymeria's chat.** Cloudflare documents that quick tunnels do not support Server-Sent Events (and cap in-flight requests at 200), so `/health` works but chat streaming breaks. Use a named tunnel (below) or Tailscale instead; verify any relay with `GET /health/stream` (events must arrive spaced out, not in one burst at the end).

### Setup: named tunnel (free Cloudflare account, stable URL)

Requires a free Cloudflare account and a domain on Cloudflare DNS (Cloudflare offers free DNS hosting for any domain you own).

```bash
cloudflared tunnel login                                    # browser flow, picks a domain
cloudflared tunnel create nymeria
cloudflared tunnel route dns nymeria nymeria.yourdomain.com
```

Create `~/.cloudflared/config.yml`:
```yaml
tunnel: <tunnel-id>
credentials-file: /home/you/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: nymeria.yourdomain.com
    service: http://localhost:8000
  - service: http_status:404
```

Install as a system service:
```bash
sudo cloudflared service install
```

Now `https://nymeria.yourdomain.com` reaches your Nymeria API. Cloudflare handles public TLS automatically.

For chat-bot credential setup links and OAuth authorization-code redirects, also set:

```bash
NYMERIA_PUBLIC_URL=https://nymeria.yourdomain.com
```

### Trade-offs

- Public traffic flows through Cloudflare's edge; Cloudflare terminates public TLS and proxies through the tunnel to your server.
- For most personal use this is acceptable. For sensitive deployments consider Tailscale instead.
- Cloudflare can disconnect your tunnel for policy reasons (rare for legitimate use).
- Cloudflare's proxy closes connections idle for ~100 seconds. The chat stream emits `: keepalive` SSE comments every 25 seconds of silence, so long-running tool calls survive the idle timeout.
- With a colocated tunnel every request reaches the API from one local address, so the per-IP auth-failure limiter collapses to one bucket. Set `NYMERIA_FORWARDED_ALLOW_IPS=127.0.0.1` so it keys on the real client IPs cloudflared forwards (see [configuration.md](configuration.md)).

---

## Domain + Caddy

The classic deployment: buy a domain, point its DNS at your VPS, and let [Caddy](https://caddyserver.com) handle TLS.

The [Docker stack](PRODUCTION_DEPLOYMENT.md) bundles Caddy out of the box. For the slim shape, the manual steps are:

1. Buy a domain. Point its A record at your VPS's public IP.
2. Open ports 80 and 443:
   ```bash
   sudo ufw allow 80
   sudo ufw allow 443
   ```
3. Install Caddy:
   ```bash
   sudo apt install caddy
   ```
4. Create `/etc/caddy/Caddyfile`:
   ```
   nymeria.yourdomain.com {
     reverse_proxy localhost:8000
   }
   ```
5. Reload Caddy:
   ```bash
   sudo systemctl reload caddy
   ```

Caddy auto-provisions a Let's Encrypt certificate on first request. Browse to `https://nymeria.yourdomain.com`.

For credential setup links and OAuth authorization-code redirects, set the same public origin in the Nymeria environment:

```bash
NYMERIA_PUBLIC_URL=https://nymeria.yourdomain.com
```

### When this is the right answer

- You're running a production or business deployment
- You want a permanent, professional URL
- You want full control without third-party services in the request path

For multi-user production, prefer the [Docker stack](PRODUCTION_DEPLOYMENT.md). It bundles Caddy plus the hardening you want.

---

## Combining paths

These paths are not mutually exclusive.

- **Telegram + Tailscale**: chat from your phone via the bot, use the desktop app from your laptop via the tailnet.
- **Cloudflare Tunnel + Telegram**: public URL for demos, chat bot for daily personal use.
- **Domain + Caddy + Telegram**: production deployment with a chat fallback.

Pick what fits each use case independently. They cost nothing to layer.
