# Remote access

Your NymeriaOS backend runs on some machine — a VPS, a home server, a Raspberry Pi. You want to talk to it from somewhere else — your laptop at work, your phone on mobile, a teammate's machine.

This page covers four paths, ordered roughly from simplest to most powerful. **You can use more than one at a time** — Telegram + Tailscale is a common combination.

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

NymeriaOS ships ready-to-use integrations for Telegram, Discord, Slack, Matrix, Signal, Mattermost, Zulip, and Rocketchat. Each one works by having a bot daemon make *outbound* connections to the chat platform's API — long-polling for Telegram, Socket Mode for Slack, Gateway for Discord, sync loop for Matrix, and so on. The chat platform routes messages between you and your bot.

**No inbound network access is needed.** Your backend can sit behind any router, NAT, firewall, or ISP that allows outbound HTTPS. No domain, no port forwarding, no tunnel, no TLS certificate.

### Why this is so good for personal use

- You can talk to your assistant from your phone, your watch, your work laptop — anywhere the chat app is installed.
- Your backend's IP and presence are never exposed to anyone but the chat platform.
- The chat app provides authentication for free — only people you've shared the bot with can talk to it.
- It works equally well in the slim shape (run as a separate Python process) and the Docker stack (run as a container).

### Setup (Telegram example)

1. Open `@BotFather` on Telegram, send `/newbot`, follow the prompts.
2. Save the bot token it gives you.
3. In your `.env` or `.env.docker`, set:
   ```
   TELEGRAM_BOT_TOKEN=<token>
   TELEGRAM_BOT_USER_ID=<your-telegram-user-id>
   ```
4. Start the bot:
   - **Slim shape:** `python run.py telegram-bot` (in a separate terminal or as a systemd unit)
   - **Docker stack:** the bot starts automatically when `TELEGRAM_BOT_TOKEN` is set
5. Open a chat with your bot in Telegram. Send `/start`.

For other chat platforms see [telegram-bot.md](../telegram-bot.md), [discord-bot.md](../discord-bot.md), [slack-bot.md](../slack-bot.md), [matrix-bot.md](../matrix-bot.md), [signal-bot.md](../signal-bot.md), etc.

### When bots alone aren't enough

- You want the full web/desktop UI with live streaming tool output.
- You want to upload large files or images beyond what the chat app allows.
- You want a shareable URL for demos or collaboration.

For those, combine bots with one of the options below.

---

## Tailscale

[Tailscale](https://tailscale.com) is a peer-to-peer VPN built on WireGuard. Install it on each of your devices, they get private IPs like `100.x.x.x`, and they can reach each other directly with end-to-end encryption.

**This is the recommended path for personal use** — backend at home, devices everywhere.

### Setup

On the NymeriaOS server:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Follow the URL printed to authenticate (uses your Google / GitHub / Microsoft account).

Then expose NymeriaOS to your tailnet with automatic HTTPS:
```bash
sudo tailscale serve --bg --https=443 http://localhost:8000
```

This:
- Binds NymeriaOS to your tailnet's MagicDNS hostname (e.g. `nymeria.your-tailnet.ts.net`)
- Provisions a Let's Encrypt TLS certificate automatically
- Makes it reachable only from devices on your tailnet

On each client device (laptop, phone, etc.):
1. Install Tailscale from https://tailscale.com/download
2. Log in with the same account
3. Open `https://nymeria.your-tailnet.ts.net` in a browser, or point nymeria-desktop at that URL

That's it. Zero public exposure, end-to-end encrypted, no domain required, no port forwarding.

### When Tailscale is the right answer

- You access NymeriaOS from your own devices and nobody else's
- You don't want to expose anything to the public internet
- You're comfortable with ≤3 users (free tier) or a small paid plan

### When it isn't

- You want a URL anyone on the internet can hit
- You have many users not under your control

### Self-hosting the control plane

Tailscale's coordination server is a third-party service. If you want zero third-party involvement, [Headscale](https://github.com/juanfont/headscale) is an open-source self-hostable replacement that speaks the Tailscale protocol.

---

## Cloudflare Tunnel

Cloudflare Tunnel runs a small daemon (`cloudflared`) on your server. It opens an outbound connection to Cloudflare's edge network. Public requests hit Cloudflare, get routed through the tunnel, and arrive at NymeriaOS. **No port forwarding, no public IP, no domain required for the free tier.**

### Setup — quick tunnel (no account, ephemeral URL)

```bash
# Install cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o cloudflared.deb
sudo dpkg -i cloudflared.deb

# Run a quick tunnel
cloudflared tunnel --url http://localhost:8000
```

Cloudflare prints a URL like `https://random-words-here.trycloudflare.com`. Anyone with that URL can reach your NymeriaOS. **The URL changes every restart** — fine for one-off sharing, not for daily use.

### Setup — named tunnel (free Cloudflare account, stable URL)

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

Now `https://nymeria.yourdomain.com` reaches your NymeriaOS. Cloudflare handles TLS automatically.

### Trade-offs

- All your traffic flows through Cloudflare's edge — they decrypt it to serve TLS.
- For most personal use this is acceptable. For sensitive deployments consider Tailscale instead.
- Cloudflare can disconnect your tunnel for policy reasons (rare for legitimate use).

---

## Domain + Caddy

The classic deployment: buy a domain, point its DNS at your VPS, and let [Caddy](https://caddyserver.com) handle TLS.

The [Docker stack](../PRODUCTION_DEPLOYMENT.md) bundles Caddy out of the box. For the slim shape, the manual steps are:

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

### When this is the right answer

- You're running a production or business deployment
- You want a permanent, professional URL
- You want full control without third-party services in the request path

For multi-user production, prefer the [Docker stack](../PRODUCTION_DEPLOYMENT.md) — it bundles Caddy plus the hardening you want.

---

## Combining paths

These paths are not mutually exclusive.

- **Telegram + Tailscale** — chat from your phone via the bot, use the desktop app from your laptop via the tailnet.
- **Cloudflare Tunnel + Telegram** — public URL for demos, chat bot for daily personal use.
- **Domain + Caddy + Telegram** — production deployment with a chat fallback.

Pick what fits each use case independently. They cost nothing to layer.
