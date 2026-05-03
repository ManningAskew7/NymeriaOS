# Twitch bot — thin-client migration decision

Status: deferred as not feasible in the original scope. The Twitch bot remains
an intentionally in-process agent client until a separate tool-proxy/RPC layer
exists.

## Why

`twitch_bot.py` is the last trigger that instantiates a full in-process `NymeriaAgent` (`run.py:160`). Discord and Telegram bots are thin clients — they talk to the API over HTTP via `NymeriaAPIClient`. Twitch doesn't.

This is the architectural smell that produced the watchdog ghost of 2026-04-17: a container with a long uptime hosting an in-process agent, whose stale in-memory code kept running a background thread (`NymeriaAgent.__init__` used to spawn `Watchdog`) and writing to shared state (`/data`, Postgres, Telegram bot token) invisibly — grep across all containers couldn't find the producer because the code had been deleted from disk weeks earlier. Commit `8811869` removed the specific background thread that caused that incident, but the *pattern* is still present: any new background worker added to `NymeriaAgent` in the future will silently duplicate itself inside the Twitch container.

Other concrete consequences of Twitch staying fat:

- Twitch drifts behind the repo whenever its container isn't restarted; behavior can diverge from `api`/`worker` in subtle ways visible only on Twitch.
- Every refactor to `core/agent.py`, `core/ticker.py`, or tool imports has to remember Twitch in its "restart these" list. Easy to miss.
- Twitch holds its own Postgres checkpointer connection and its own tool registry — doubling connection count and creating a second source of truth for what tools are enabled.

## What a full thin-client migration would require

The Discord/Telegram pattern is not enough by itself:

1. Replace the in-process `NymeriaAgent` with a `NymeriaAPIClient` (`triggers/api_client.py`) pointed at `http://nymeria-api:8000`.
2. Route chat through `POST /chat` (streaming SSE), not in-process agent streaming.
3. Pull settings, model lists, and per-thread config through API endpoints — not direct Postgres reads.
4. Remove `NymeriaAgent(...)` from `run_twitch_bot` in `run.py`. Twitch becomes pure I/O glue.

That partial migration would break Twitch tool execution. In Nymeria, tool
calls execute inside the agent's ReAct loop. If the loop runs in the API
container, `twitch_send`, `twitch_read_chat`, moderation tools, and broadcaster
actions also run there. The API process has no registered `TwitchToolRuntime`
bot and cannot access the bot's in-memory chat buffer, TwitchIO event loop,
auto-refreshed OAuth token store, or resolved broadcaster/bot IDs.

A real migration needs a new remote tool runtime first:

1. An authenticated HTTP/RPC surface inside the Twitch bot container for chat
   send/read, user resolution, Helix requests, and runtime status.
2. An API-side `TwitchToolRuntime` mode that proxies each Twitch tool call to
   that bot-local service and preserves current error semantics.
3. Docker networking, service-token or narrower shared-secret auth, retry/time
   limits, and startup health checks for the cross-container tool path.
4. Tests proving the API-side agent can execute `twitch_send`,
   `twitch_read_chat`, and at least one moderation/Helix tool without a local
   TwitchIO bot object.

## Why Twitch is different from Discord/Telegram

Discord and Telegram bots are thin clients because their agent tool calls do
not need direct access to a live bot process. Twitch tools do.

The in-process dependencies are:

- **Chat buffer + pulse**: `twitch_read_chat` reads the bot's in-memory ring
  buffer, and the bot batches unseen messages before invoking the agent.
- **Outbound chat and EventSub state**: `twitch_send` schedules work onto the
  TwitchIO bot loop and uses resolved broadcaster/bot IDs.
- **Moderation and broadcaster actions**: tools use TwitchIO's current,
  auto-refreshed token store and Helix access.
- **Stop/start safety**: `TwitchToolRuntime` checks the bot's local stopped
  state before executing write actions.

Per-thread config and model selection are not the blocker; existing API
endpoints can already cover most of that state. Tool execution locality is the
blocker.

Docs: `Nymeria/docs/twitch-bot.md` covers the current in-process
architecture and the reason a bot-side tool proxy must come before any
thin-client migration.

## Current decision

The known-risky background thread (`Watchdog`) is gone as of `8811869`, and
`run.py twitch-bot` creates its agent with `enable_ticker=False`. TOOL-006
also moved Twitch tools behind `core/twitch_runtime.py`, so tool hot-reload no
longer captures stale module-global bot references.

For the current single-developer deployment, the remaining in-process agent is
acceptable. Revisit the full proxy design only if horizontal deployment,
multiple Twitch bot instances, or API-side centralized tool execution becomes a
real requirement.
