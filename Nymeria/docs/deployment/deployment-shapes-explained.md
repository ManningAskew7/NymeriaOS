# Deployment Shapes Explained

A beginner-friendly tour of the two deployment shapes (slim and Docker),
the moving parts inside each, and why both exist. Read this before
[README.md](deployment-README.md) if the terms "SSE", "Redis", or
"Postgres vs SQLite" are not already comfortable for you.

If you already know what those terms mean and just want to pick a shape,
skip to [README.md](deployment-README.md). If you are operating a
Docker deployment, see [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md).
Either Docker shape (the single container or the full stack) builds its images
from a source checkout: the beta publishes no container images, so there is
nothing to pull.

## The one-sentence version

Slim runs every piece of Nymeria as one Python process talking to local
SQLite files. The Docker stack runs the same code split across several
container processes that share a Postgres database and a Redis message
bus. Same features, different envelope.

## The pieces inside Nymeria

Whichever shape you pick, the same five subsystems are running. The
difference is whether they are bundled into one process or split across
several.

| Subsystem | What it does |
|---|---|
| API | Serves HTTP requests, runs the agent for each chat turn, holds open SSE connections to frontends |
| Ticker | Polls scheduled TODOs and trigger sources, dispatches autonomous agent turns when something is due |
| Watchdog sweep | Ticker sub-loop that nudges threads about stale TODOs (no update past the staleness window) and sends off-frontend alerts |
| MCP server | Exposes Nymeria as 52 tools to external LLM clients (Claude Desktop, etc.) |
| Storage | Conversations, accounts, credentials, TODOs, memory, profiles |

In slim, all five run inside one `python3 run.py slim` process.
In Docker, each runs in its own container.

## What is SSE and why does it matter

SSE stands for Server-Sent Events. It is the protocol the frontends use
to receive a streaming response from the agent.

A normal HTTP request is like a phone call: you ask, the server answers,
you hang up. That model does not work for chatting with an AI, because
the AI is generating the response one chunk at a time. If the frontend
waited for the whole response, the user would stare at a blank screen
for several seconds and then see a wall of text.

SSE keeps the connection open and lets the server push new chunks down
the pipe as they happen. Each event has a type ("thinking",
"tool_call", "response", "task_completed") and a payload. The frontend
displays them as they arrive, which is what creates the typewriter
effect of words appearing live.

In Nymeria, every frontend (desktop, mobile, web, CLI) opens an
SSE connection to the API at `/autonomous/stream` as soon as the user
logs in, and keeps it open the whole time the user is active. Anything
the API needs to tell the frontend gets pushed down that pipe.

## What is Redis and what does it do here

Redis is a separate program that runs all the time and acts as a shared
scratchpad for other programs. Multiple Python processes on the same
host (or across hosts) can read and write to Redis, and Redis handles
the coordination so they do not bump into each other.

Nymeria uses Redis for exactly one thing: **pub/sub messaging between
the worker container and the API container.**

Picture a restaurant. You are sitting at a table (your frontend). The
waiter is the API container, standing at your table holding your SSE
connection open. The chef is the worker container, cooking your food
(running scheduled TODOs and trigger fires) in a separate kitchen. The
chef cannot walk out to your table. The waiter cannot leave your table
to check the kitchen. They need a runner to shuttle status updates
back and forth.

Redis is that runner.

Concretely: when the worker runs a scheduled TODO, it publishes
`task_started`, stream chunks, and `task_completed` events to a Redis
channel. The API container is subscribed to that channel. When the API
receives an event, it forwards it to any local SSE subscriber whose
user matches. Your frontend sees the live progress of the autonomous
turn even though the agent runtime never ran in the same container that
holds your SSE connection.

In slim, the ticker and the API are in the same process, so the same
event bus is just a Python dict. No Redis. No network hop. Same
functional outcome.

## What is SQLite vs Postgres

Both are SQL databases. They speak almost the same query language. The
structural difference is how they live on the system.

**SQLite is a file on disk.** Your Python code opens a `.db` file
directly and reads or writes it. There is no server. Like a paper
notebook on your desk: you open it, write in it, close it.

**Postgres is a separate program that runs all the time and accepts
queries over a network connection.** You never touch the data file
yourself. You ask Postgres "please write this row" and it does the
work. Like a librarian behind a desk: you do not go to the shelves,
you hand the librarian a request and they bring back the result.

Why does this matter? **Concurrent writes.**

SQLite holds a file-level write lock. Writer #1 takes the lock, finishes
writing, releases it; only then does writer #2 get to go. WAL mode lets
readers keep reading during writes, but you still get one writer at a
time per database file.

Postgres handles many simultaneous writers natively. Each connection
gets its own transaction; the database itself coordinates row-level
locking. Many users can be writing different rows in parallel without
queueing.

So the rule of thumb:
- **One process, light traffic**: SQLite is simpler and faster (no
  network overhead).
- **Many processes or many users writing at once**: Postgres scales
  better because it does not serialize writers.

### What actually uses Postgres in Nymeria

This is the part that surprises most people who dig in. In the Docker
stack, **only the LangGraph conversation checkpoint table uses
Postgres.** Every other persistence layer is still SQLite, on a shared
Docker volume that all containers can read:

| Subsystem | Storage in slim | Storage in Docker |
|---|---|---|
| Conversation checkpoints (LangGraph) | SQLite | **Postgres** |
| Accounts, tokens, ownership | SQLite | SQLite |
| Credential vault | SQLite | SQLite |
| Notification destinations | SQLite | SQLite |
| Chat platform bindings | SQLite | SQLite |
| Per-user RAG/memory (`memory.db`) | SQLite | SQLite |
| Skill embedding index | SQLite | SQLite |
| TODO schedule + active execution markers | SQLite | SQLite |
| User profiles | JSON files | JSON files |

So Postgres in this stack is a targeted optimization for one specific
write-heavy table, not a wholesale storage swap. RAG memory uses
SQLite-only features (FTS5 full-text search and the `sqlite-vec`
extension), so it stays on SQLite even at scale.

This means the practical write-contention ceiling of Docker is higher
than slim, but Docker does not magically make all writes "scalable".
It just lifts the ceiling on the busiest table.

## Why two shapes exist

Once you understand the pieces, the reason for two shapes is plain:
**process isolation costs simplicity, and which trade-off is right
depends on whether you are serving yourself or serving other people.**

### Slim: one process

When everything runs in one process, communication is free (Python
function calls), the storage is simple (SQLite files in `data/`), and
deployment is trivial (`python3 run.py slim`).

The trade-offs:
- If the one process crashes, everything stops until something
  restarts it.
- Concurrent writes from many users queue up on SQLite file locks. The
  practical ceiling is around 10 active users before this starts to
  hurt.
- TLS, reverse proxying, and public hostnames are your problem.

### Docker stack: several processes

When the work is split into separate containers, each container can
crash and restart independently without taking the others down. Postgres
absorbs the conversation-write hotspot. Caddy handles HTTPS. The worker
container handles autonomous work without competing with live HTTP
traffic for the API container's resources.

The trade-offs:
- More moving parts to operate.
- The separated processes need a bus to talk to each other (Redis).
- The separated processes need a shared database server (Postgres).
- The agent's filesystem is the container's, not the host's. A slim
  instance shares the host filesystem, so "read this file" with a host
  path works there and fails in Docker unless that path is mounted; the
  same prompt genuinely behaves differently on the two shapes. The file
  tools say so on a deep not-found (rather than a bare "does not
  exist"), and the practical alternatives are pasting the content into
  the conversation or targeting an instance that runs on that machine.

These costs are worthwhile when you are serving more than yourself or
when uptime matters.

## How a scheduled TODO actually flows through each shape

This is the clearest concrete example of the difference.

### In slim

1. The in-process ticker notices a TODO is due
   (`nymeria/core/ticker.py`).
2. It calls the agent directly via `LocalAgentExecutor`
   (`nymeria/core/turn_executor.py`). Same
   process, no network hop.
3. As the agent produces chunks, the ticker publishes them through
   `publish_agent_stream_chunk` to the in-memory event bus
   (`nymeria/core/event_bus.py`).
4. The SSE generator handling the user's `/autonomous/stream`
   connection drains the queue and pushes each chunk to the frontend.

Five function calls. No network. No Redis. No serialization.

### In Docker

1. The worker container's ticker notices a TODO is due.
2. The ticker uses `APIClientExecutor` to call `POST /chat` on the API
   container, passing `publish_autonomous_events=False` so the API
   suppresses its own bookend events. The agent runs inside the API
   container. Stream chunks come back to the worker as the HTTP SSE
   response.
3. As each chunk arrives at the worker, the worker republishes it via
   `RedisEventBus.publish`
   (`nymeria/core/event_bus_redis.py`)
   using stable task IDs (`todo.id`).
4. The worker's publisher-only `RedisEventBus.publish` skips local SSE
   dispatch because the worker has no local SSE subscribers, then
   publishes to the Redis
   `nymeria:autonomous_events` channel.
5. The API container's `RedisEventBus` subscriber thread receives the
   message, drops self-echoes, and dispatches to local SSE subscribers.
6. The SSE generator drains the queue and pushes each chunk to the
   frontend.

The end result is identical. The wiring is the only thing that differs.

## What this means for someone modifying the code

If you change anything that touches scheduled work, the event bus, or
storage, your change has to work in both shapes. A few rules that come
out of the architecture:

- **Always publish through the global helpers**
  (`publish_autonomous_event`, `publish_agent_stream_chunk`,
  `publish_sync_event`). Do not import `RedisEventBus` directly. The
  in-memory and Redis buses are interchangeable behind that interface.
- **Never assume a separate process exists.** In slim, the ticker
  (including the watchdog sweep), the MCP server, and the API are all
  the same process. New periodic background work should ride the ticker
  as a sub-loop (`core/ticker.py` `_maybe_submit_*`; the watchdog sweep
  at `core/watchdog_sweep.py` is the template), which lands it in the
  right process in both shapes with zero shape-specific wiring.
- **Branch on `settings.database_backend` only where Postgres is
  actually used.** The known sites are `checkpointer_config.py`,
  `thread_branch.py`, and `api/routers/threads.py`. Adding new branches
  elsewhere usually indicates a design problem.
- **Per-thread locking is process-local on purpose.** The agent runs
  in exactly one process in both shapes (slim's API process, Docker's
  API container). The Docker worker no longer constructs a
  `NymeriaAgent`; it relays turns through HTTP. Do not add features
  that assume in-memory locks can be shared across processes, because
  that would require a real distributed lock manager that does not
  exist today.
- **`MAX_CONCURRENT_AUTONOMOUS` and the ticker pool are identical in
  both shapes.** Same setting, same enforcement site, same default
  (5). Only the executor differs.

## Where to go next

- [README.md](deployment-README.md): the chooser (which shape do I
  want?) and side-by-side comparison
- [slim.md](deployment-slim.md): the slim launcher reference
- [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md): Docker
  operator's manual
- [architecture.md](../getting-started/architecture.md): full agent runtime internals
  for engineers
- [remote-access.md](deployment-remote-access.md): accessing either
  shape from outside the host
