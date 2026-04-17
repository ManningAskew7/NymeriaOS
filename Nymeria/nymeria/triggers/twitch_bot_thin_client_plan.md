# Twitch bot — planned thin-client refactor

## Why

`twitch_bot.py` is the last trigger that instantiates a full in-process `NymeriaAgent` (`run.py:160`). Discord and Telegram bots are thin clients — they talk to the API over HTTP via `NymeriaAPIClient`. Twitch doesn't.

This is the architectural smell that produced the watchdog ghost of 2026-04-17: a container with a long uptime hosting an in-process agent, whose stale in-memory code kept running a background thread (`NymeriaAgent.__init__` used to spawn `Watchdog`) and writing to shared state (`/data`, Postgres, Telegram bot token) invisibly — grep across all containers couldn't find the producer because the code had been deleted from disk weeks earlier. Commit `8811869` removed the specific background thread that caused that incident, but the *pattern* is still present: any new background worker added to `NymeriaAgent` in the future will silently duplicate itself inside the Twitch container.

Other concrete consequences of Twitch staying fat:

- Twitch drifts behind the repo whenever its container isn't restarted; behavior can diverge from `api`/`worker` in subtle ways visible only on Twitch.
- Every refactor to `core/agent.py`, `core/ticker.py`, or tool imports has to remember Twitch in its "restart these" list. Easy to miss.
- Twitch holds its own Postgres checkpointer connection and its own tool registry — doubling connection count and creating a second source of truth for what tools are enabled.

## What thin-client migration looks like

Mirror the Discord/Telegram pattern:

1. Replace the in-process `NymeriaAgent` with a `NymeriaAPIClient` (`triggers/discord_api_client.py`) pointed at `http://nymeria-api:8000`.
2. Route chat through `POST /chat` (streaming SSE), not `agent.stream()`.
3. Pull settings, model lists, and per-thread config through API endpoints — not direct Postgres reads.
4. Remove `NymeriaAgent(...)` from `run_twitch_bot` in `run.py`. Twitch becomes pure I/O glue.

## What makes Twitch harder than Discord/Telegram

Three features currently rely on in-process agent access and need API-side support first:

- **Chat buffer + pulse**: Twitch aggregates rapid chat messages into a single agent invocation. Today this happens by buffering locally and calling `agent.stream(...)` directly. Thin-client version needs a `POST /chat` that accepts a batch of messages, or the buffering stays client-side but flushes to `/chat` on pulse.
- **Per-thread model override**: Twitch lets different channels use different LLMs. `PATCH /threads/{id}/config` already supports this — likely a straight port, but needs verification that all the Twitch-specific fields are covered.
- **Async voice pipeline**: audio transcription → LLM → TTS is driven locally. The LLM step needs to flow through `/chat`, but the audio I/O stays in the Twitch container. Streaming SSE should be sufficient; the pipeline is already async.

Docs: `Nymeria/docs/twitch-bot.md` covers the current fat-client architecture and should be updated as part of the refactor.

## Priority

**Not urgent.** The known-risky background thread (`Watchdog`) is gone as of `8811869`. This refactor is architectural hygiene — schedule it before adding any new background component to `NymeriaAgent`, or if Twitch behavior diverges from Discord/Telegram for the same input.
