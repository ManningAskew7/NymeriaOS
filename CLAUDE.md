# CLAUDE.md

<!-- Canonical agent guide for this repository. Keep factual and current when
     directories, runtime shapes, commands, or doc ownership change. Paths are
     repo-relative and host-agnostic. Avoid em dashes. Kept byte-identical to
     the AGENTS.md beside it. -->

Code map for agents and contributors working in this repository. Read the
sections and docs that match the task in front of you; do not bulk-read
`Nymeria/docs/`.

## Project Context

- NymeriaOS is a pre-1.0 personal AI assistant platform: a Python agent
  backend plus desktop, mobile, web, and chat-platform clients. This file is
  the code map; `Nymeria/docs/getting-started/PROJECT_BRIEF.md` is the product
  overview.
- Licensed PolyForm Noncommercial 1.0.0 (`LICENSE`). The published trust model,
  including its accepted concessions, is `SECURITY.md`; read it before
  changing anything that spawns a process, egresses a credential, or gates a
  tool.
- The product name is NymeriaOS. Internal identifiers stay `nymeria`: the CLI
  command, the Python package `Nymeria/nymeria/`, `NYMERIA_*` environment
  variables, container names, and `Nymeria/nymeria/config/soul.md`.
- Local operational state (databases, tokens, `.env*`, gitignored scratch
  dirs) exists by design on a running install. Treat it as user-owned: do not
  open, print, edit, move, or delete it unless asked, but do not block
  ordinary work because it shows in `git status`.
- Facts marked "(may have changed)" are snapshots; verify them against the
  code when a task depends on exact values.
- Much of this codebase was written by earlier, weaker models working to good
  practice, and the architecture holds up. Do not treat "idiomatic here" as
  sacred: when you find a real problem in code adjacent to what you are
  touching, fix it rather than replicate it.

## Rules

The most important global rules:

1. No em dashes and no double hyphens as punctuation, in documentation or in
   code, especially user-visible strings. Use commas, periods, parentheses, or
   colons.
2. `git pull --ff-only` before starting any implementation or docs work. If the
   checkout has uncommitted work, use a temp worktree from `origin/main`.
3. New features and fixes ship with tests, written and run, following nearby
   test patterns, and the tests must have teeth. Prove each new test can fail:
   for a bug fix, red on the unfixed code first; for a feature, break what each
   test covers and see it red once. Assert observable outcomes rather than
   internal calls, and sweep the edge cases that apply. Shape-only asserts
   (`is not None`, a bare status code) are never the payoff. A test that still
   passes with the feature broken is a defect in the test. New code is written
   for current intent, never contorted to keep an existing test green; a
   failing existing test is triaged as a regression unless shown otherwise.
   While iterating, run only the test files relevant to the change; run the
   full backend suite once, before committing (it is slow, roughly 9 minutes).
   ruff and pyrefly are fast, run them throughout.
4. Keep this guide current: new features and files get a concise map row. Edit
   the relevant line or row in place; do not append prose. Prefer a pointer
   over inline detail: a row says what a thing is plus its traps, the doc
   carries the derivation. Update the matching doc in `Nymeria/docs/` when code
   behavior changes. `AGENTS.md` is a byte-identical twin of this file; edit
   both.
5. No secrets in the tree. Runtime secrets live in gitignored `.env*` files or
   the credential vault (`Nymeria/nymeria/core/credential_vault.py`), never in
   source, tests, or docs. `scripts/check_no_plaintext_secrets.py` is the gate.
6. Read enough code to place a change in the idiomatic module before writing
   it. Bug fixes need evidence (code, logs, API output, tests) before patching.
   Documentation-only tasks change no application code.

Git flow: stage specific files only, never `git add -A`; commit messages
follow `type(scope): summary`. Never revert another contributor's changes
unless asked.

## Repository Layout

| Top level | What it is |
| --- | --- |
| `Nymeria/` | Python backend: the `Nymeria/nymeria/` package, `Nymeria/run.py`, `Nymeria/tests/`, docs in `Nymeria/docs/`, compose files and Dockerfiles |
| `nymeria-desktop/` | Desktop client: Svelte 5 + Tauri 2 (Rust shell); also the source of the built web UI |
| `nymeria-mobile/` | Mobile client: Svelte 5 + Capacitor 6 |
| `scripts/` | Cross-app checks and the frontend bundling script (see Quick Commands) |
| `CLIProxyAPI-main/` | CLIProxy config template only (`CLIProxyAPI-main/temp/latest/config.yaml.example`); the proxy itself is pulled as a Docker image (see CLIProxy Warning) |
| `website/` | Standalone landing page (`website/nymeriaos-landing-standalone.html`) |
| `.github/workflows/` | `ci.yml` (secret guard, version and requirements sync, ruff, pyrefly, pytest with coverage, desktop type check), `release.yml`, `windows-smoke.yml`, `claude-review.yml` (Claude Code review after CI on main pushes and PRs; answers `@claude` in comments) |
| Root docs | `CLAUDE.md`/`AGENTS.md` (this guide, kept identical), `LICENSE`, `SECURITY.md`, `install.sh`/`install.ps1` (install front doors) |

## Environments

The backend runs in more than one shape and on more than one OS, so prefer
repo-relative paths and portable invocations over absolute paths.

- Python 3.11 or newer. Invoke as `python3`/`pip3`, and run tools as
  `python3 -m ruff`, `python3 -m pytest`.
- Node is needed for either client; Tauri desktop builds also need a Rust
  toolchain.
- Docker is the primary multi-process runtime and the integration-test
  environment. Two runtime shapes (slim and Docker) run the same code paths;
  new backend features must work in both.
- Desktop and mobile dev usually runs against a remote backend; the Tauri app
  defaults to thin-client mode with no local backend
  (`Nymeria/docs/frontends/desktop-client-only.md`).

## Quick Commands

Backend, from `Nymeria/`:

```
python3 run.py <cmd>    # api cli slim worker mcp claude-code-runner service init doctor reembed users snapshot completion
python3 run.py <p>-bot  # discord telegram slack twitch
```

(whatsapp and teams are webhook routers in `Nymeria/nymeria/api/routers/`, not
subcommands.)

Docker stack, from `Nymeria/` (first copy `Nymeria/.env.docker.example` to
`.env.docker`; it must supply `POSTGRES_PASSWORD` and `REDIS_PASSWORD`):

```bash
docker compose --env-file .env.docker up -d --build
docker compose --env-file .env.docker restart api worker mcp
docker compose --env-file .env.docker --profile <name> up -d   # discord telegram twitch slack voice voice-gpu search
docker logs nymeria-api --tail 50
docker exec nymeria-postgres psql -U nymeria -d nymeria
curl http://localhost:8000/health
```

Python source is bind-mounted, so code changes need only `restart`; changes to
`requirements*.txt`, `Dockerfile.*`, or `NYMERIA_LOCAL_RAG` need
`up -d --build`. `Nymeria/docker-compose.single.yml` is containerized slim
(SQLite, no Postgres, Redis, or worker), built from the checkout;
`Nymeria/docker-compose.single.published.yml` is the same shape pulling a
published image (no images are published during the beta, so build from
the checkout).

Checks:

```bash
python3 -m pytest tests/                   # backend, from Nymeria/; add -n auto (or -n 2 on small hosts)
python3 -m ruff check .                    # from Nymeria/
pyrefly check                              # from Nymeria/
npm run check                              # svelte-check, from nymeria-desktop/ or nymeria-mobile/
npm run test                               # Vitest, from nymeria-desktop/ or nymeria-mobile/
python3 scripts/check_cross_app_drift.py   # repo root: desktop vs mobile drift gate
python3 scripts/sync_versions.py           # repo root: version sync
```

`scripts/` also holds `check_no_plaintext_secrets.py`,
`check_requirements_sync.py` (pyproject vs requirements names),
`audit_contrast.py` (WCAG theme audit), and `build_frontend.sh` (rebuilds the
desktop bundle and copies it into `Nymeria/nymeria/frontend/`). Backend
utilities live in `Nymeria/scripts/` (`generate_tools_index.py`,
`generate_discord_cogs.py`, `refresh_pricing_snapshot.py`,
`update-image-digests.sh`, more) and operational probes in `Nymeria/tools/`
(`check_cliproxy_cloak.py`, `rag_eval.py`, `inspect_thread.py`, more).

## Runtime Shapes

Two supported shapes run the same code paths behind a `TurnExecutor`
(`Nymeria/nymeria/core/turn_executor.py`): `LocalAgentExecutor` in-process,
`APIClientExecutor` relaying turns through the API. The split buys process
isolation and write concurrency, not extra capability.

| Area | Slim (`python3 run.py slim`) | Docker stack |
| --- | --- | --- |
| Processes | One process: API, ticker, MCP | Containers: api, worker, mcp, bots, infra |
| Database | SQLite under `Nymeria/data/` | PostgreSQL (LangGraph checkpoints only) |
| Event bus | In-memory | Redis pub/sub (relays autonomous SSE events only) |
| Service token | Auto-minted `data/SLIM_SERVICE_TOKEN.txt` | `NYMERIA_SERVICE_TOKEN`, or the API self-mints to the shared volume when unset |
| MCP | Mounted at `/mcp` in the API process | `nymeria-mcp` container (port 8001, may have changed) |
| Agent execution | API process | API process |

Invariants across both shapes:

- The API process is the only agent runtime. The Docker worker schedules TODOs
  and relays turns to the API; it runs no `NymeriaAgent`. The worker is
  deliberately absent from `run.py`'s token-required command list (it starts
  before the API mints the shared token): do not "fix" that.
- In-process coordination (`ThreadLockManager`, `PendingPromptQueue`) stays
  authoritative because the API is the single agent runtime.
- Internal callers use `NYMERIA_API_URL` plus the service token. Slim registers
  its pins (loopback, `DATABASE_BACKEND=sqlite`, `REDIS_ENABLED=false`,
  `REDIS_URL` removed) so no later env read undoes them
  (`run.py::_apply_slim_runtime_env`).

Before touching the event bus, storage, scheduled work, or any background
loop, read `Nymeria/docs/deployment/deployment-shapes-explained.md`.

## Backend Code Map

Package root: `Nymeria/nymeria/`. Paths in this table are relative to it.

| Path | Purpose |
| --- | --- |
| `Nymeria/run.py` | Single argparse entry point for every Quick Commands mode. `cli_entry.py` is the packaged console-script wrapper; `_runtime_paths.py` resolves the project root for package vs source launches |
| `core/` | Platform services: thread lifecycle, config, and locks; compaction; TODOs plus ticker and its sub-loops; triggers (`trigger_manager.py`); accounts, `credential_vault.py`, rate limiting; the event bus (in-memory and Redis) and `turn_stream_buffer.py`; notifications and FCM; `completion_delivery.py` (detached delivery for bash, Claude Code, and callable results); approval and rendezvous records; MCP runtime (`mcp_*.py`); `custom_tool_gate.py` (whole-definition execution gate for all custom-tool types); the tool-search index and embedding cache; `snapshot*.py` (backup and restore); `child_teardown.py`; `resource_map.py` (the register of agent-writable stores, ratcheted by `Nymeria/tests/test_resource_layout.py`). See `core/README.md` |
| `core/agent.py` + `core/agent_*.py` | `NymeriaAgent` orchestration plus its extraction family (graph, tools, prompt, llm_config, context, compaction, prune, history, streaming, turn_loops, turn_metadata). Gotchas: intra-module calls route through `agent._facade_method(...)` to keep test monkey-patch seams; siblings import agent symbols function-locally to dodge circular imports |
| `core/agent_graph.py` | Graph build; `select_tools_for_graph` is the runtime tool-selection authority |
| `core/tool_execution.py` | The shared tool-execution POLICY (gate, resolver, PRE and POST hook sandwich, `ToolDenied`) for the graph tool node, `tool_invoke`, the `nym.*` verbs, and `self_invoke_tool`. It is NOT a universal choke point: other surfaces execute tools without it, so never design a control that assumes one |
| `core/turn_executor.py` | `TurnExecutor` protocol with `LocalAgentExecutor` and `APIClientExecutor`, plus the headless `run_workflow` seam |
| `core/command_service.py` | Central slash-command dispatch shared by CLI, API, and bots; catalog in `core/registry_defaults.py`. New handler families go in `command_executor_*.py` mixins, never in the service. Arguments are DECLARED: `core/command_params.py` owns `CommandParam`, `bind_args`, and generated usage; option lists resolve through one registry (`core/command_option_resolvers.py`). `surfaces` filters discovery only; `blocked_surfaces` is the enforced axis. Output caps are applied once per surface at dispatch |
| `core/dreaming/` | Dream shadow threads (self-reflection): `invoke.py` runs dream turns, `scheduler.py` is the eligibility sweep, agent surface in `tools/dream_tools.py`, prompts in `config/dream_prompt.md` and `config/dream_kickoff.md`. Doc: `Nymeria/docs/agent-systems/dreaming.md` |
| `core/hooks/` | Lifecycle-hooks engine: `base.py` (contract), `registry.py`, `scratch.py`, `dispatch.py`, `actions.py`, `bridge.py`; siblings `hook_spec.py`, `hook_manager.py`, `hook_approvals.py`, `conditions.py` (shared with triggers), `hook_templates.py` plus `hooks_bundled/`. Fault policy inverts by event: dispatch fails closed on veto events, actions default to allow-with-note. Doc: `Nymeria/docs/agent-systems/hooks.md` |
| `core/workflows/` | Workflow SDK engine (the `nym` runtime). Agent-authored Python runs OUT of process (`runner.py`, scrubbed env) against a generic `nym.*` proxy answered by a parent-side RPC pump; `authoring.py` owns `revision_hash` and the per-revision execution gate, re-checked at every execution. Any user may draft; execution needs an admin-approved revision. Bundled examples: `workflows_bundled/` |
| `core/attachment_sandbox.py` | Per-thread attachment sandbox and fitted-image cache; the generated-image window is `core/generated_image_context.py`, where the byte cap SKIPS an image but the pixel ceiling (`core/image_limits.py`) downscales it |
| `core/voice.py`, `core/voice_local.py` | TTS and STT services; `voice_local.py` runs in-process Kokoro TTS and faster-whisper STT (extra: `nymeriaos[voice-local]`); bots speak via `triggers/voice_helpers.py` |
| `api/` | `api/routers/` (FastAPI routers by domain plus the webhook bot routers; whatsapp and teams share `api/routers/_bot_inprocess.py`), `api/schemas/` (Pydantic models), `sse.py` (SSE keepalive comment frames), `thread_overview.py` (per-thread read model). Interactive admission control gates every interactive turn behind the opt-in `MAX_CONCURRENT_INTERACTIVE` ceiling, returning 429 with Retry-After before the SSE handshake. See `api/routers/README.md` |
| `tools/` | Tool modules. `__init__.py` owns `SEED_TOOLS` (~16, may have changed) and derives the catalog (~1,270 static tools, may have changed), `__all__`, and the role gates from `tools/registry.py`; category and security metadata in `metadata.py`; custom HTTP and MCP schemas in `tools/definitions/`. The `*_service_integrations` modules share `service_integration_base.py`; five helpers there are CONTROLS, not conveniences (`request_with_policy`, `http_policy.policy_http_client`, `signed_endpoint_url`, `vendor_host`, `require_joined_destination`), and a module reaching httpx directly fails `Nymeria/tests/test_service_integration_egress.py`. `chrome_browser.py` drives the user's own Chrome through a browser extension: page text is fenced as untrusted. Module-level registries and live-resource singletons here must be reload-survivable |
| `triggers/` | API app factory (`api.py`), native and webhook bot clients, `sse_consumer.py` (SSE normalization), the terminal client (`triggers/cli/`), trigger source plugins (`triggers/sources/`: webhook, RSS, HTTP poll, Outlook email, Slack, Teams), and Discord cogs (mostly GENERATED from the command registry by `Nymeria/scripts/generate_discord_cogs.py`; regenerate rather than editing `triggers/discord_cogs/generated_cogs.py`). Bots consume chat via `sse_consumer.consume_chat_stream_with_recovery`: after `turn_started`, a drop re-attaches and never re-POSTs |
| `vendor/react_agent/` | Forked LangGraph ReAct runtime, owned code (fork policy in its `README.md`): `providers.py` (provider factory, `resolve_max_output_tokens`), `cliproxy.py` (detection plus cloak constants), `reasoning_passback.py`, `nodes.py` (graph nodes, per-call tool timeouts, image strip-and-retry, empty-turn guard), `graph.py` (compile plus checkpointer factory). The `max_tokens` and truncation layer carries measured asymmetries pinned at each site; read the pins before touching a cap. `anthropic_probe_base_url` is a credential-egress gate, not a convenience |
| `config/` | `settings.py` (settings source of truth), `llm_providers.py` (130+ providers), `model_capabilities.py` (effort ladders and clamps, context, vision, and document resolution), `model_tiers.py`, `oauth_providers.py`, `pricing_table.py`, `local_llm.py`, `env_file.py` (shared dotenv merge-writer that collapses duplicate keys), logging, and prompts (`soul.md` persona, dream prompts). Gotcha-dense: tier order, curated-over-catalog, and `max_tokens` vs `max_input_tokens` meaning different quantities per source, each pinned at its own site |
| `skills/`, `skills_bundled/` | Skill loader, marketplace fetcher, semantic search, and the `Skill()` meta-tool; bundled skills and Skill Kits. Kits declare `required_skills` (one-level strict resolution, union bind) and `thread_templates` (lazy callable threads). `Skill()` requires an explicit `ttl`. Doc: `Nymeria/docs/agent-systems/skills.md` |
| `agents/` | Callable-thread tool factory: threads with `callable=true` become per-user tools (`callable_name` is the routing key), plus kit-declared thread-template tools. Callable teams are isolated both ways, enforced at graph build and again at runtime, not by `team_manager.py` |
| `cliproxy/`, `gateway/`, `cli/` | CLIProxy subscription-OAuth integration (provider catalog plus async management client); the foreground gateway service mode; the account provisioning CLI (`users`) and the user-data snapshot CLI (`snapshot`) |
| `setup/` | Textual `nymeria init` wizard: `setup/steps/` (provider, model, rag, cliproxy, deployment, hosting, external access, agent settings, review, more), TUI-free catalogs, `environment.py` (host detection and hosting gates), `external_access.py` (Tailscale and Cloudflare tunnels), `local_rag_install.py`. Finalize and hydrate write env files and carry picks into Docker; `--non-interactive` reconfigures |
| `onboarding.py`, `service_install.py`, `doctor.py` | First-run choices model; systemd and launchd background-service install behind the `nymeria service` verbs; `nymeria doctor` diagnostics |
| `frontend/` | The built web UI the API serves. It is a build artifact: regenerate with `scripts/build_frontend.sh`, which is what the release workflow does |
| `subprocess_env.py`, `process_hardening.py`, `oom.py`, `exec_sandbox.py` | Package-root child-process controls, all load-bearing. `subprocess_env.py` is the single source of truth for what a spawned child inherits, and `Nymeria/tests/test_subprocess_env_gate.py` is an AST gate that fails the build on a new bare spawn. `process_hardening.py` and `oom.py` are two halves of one mechanism: the dumpable flag is inherited across fork, so the OOM preexec must restore it per child or biasing silently stops. `exec_sandbox.py` is the Landlock mechanism; `core/exec_policy.py` is its policy twin. Every constraint here was measured and pinned at its own site: do not re-derive or "simplify" it |
| `mcp_server.py`, `mcp_auth.py`, `mcp_backend_client.py` | Thin-client MCP server over the API, its auth middleware, and its HTTP backend client (see MCP Server) |

## Architecture Patterns

*Prefer the standard library, platform features, or what the project already
provides over new code or new dependencies.*

- Agents are users too: threads reprogram the platform at runtime and LLMs are
  strong authors of code and structured config, so lean toward extensible,
  composable designs over hard-coded behavior.
- Threads are the per-context configuration unit (tools, model, skills, MCP,
  dreaming, connections); per-platform behavior differences are thread config,
  not gaps.
- Tool classification: the module docstring of `Nymeria/nymeria/tools/__init__.py`
  is canonical. `SEED_TOOLS` seeds each user's editable `default_thread_tools`;
  the catalog is the opt-in bindable set. The runtime authority is
  `core/agent_graph.select_tools_for_graph`:
  `(default_thread_tools or SEED_TOOLS) | enabled_tools | temporary_tools`
  minus `disabled_tools` (authoritative) and role gates. Catalog tools bind by
  name at graph build, not through the ToolRegistry, and the catalog, count,
  and role gates derive from `tools/registry.py` rather than being hand-listed.
  Prefer a catalog group over `SEED_TOOLS`. Flat generated list:
  `Nymeria/docs/agent-systems/tools-index.md`; concepts:
  `Nymeria/docs/agent-systems/tools.md` and
  `Nymeria/docs/agent-systems/tool-hot-loading.md`.
- Skill Kits bind required tools with TTLs and can nest skills and kits. User
  slash commands: `/skill <name>` and `/kit <name>`.
- Slash commands register in the backend registry (`core/command_service.py`
  applying `core/registry_defaults.py`); frontends forward `/command` input to
  `POST /commands/execute` and register locally only for genuinely
  frontend-local behavior (redraw, clipboard, theme, haptics).
- LLM providers are registry-backed in `config/llm_providers.py`; creation
  flows through `vendor/react_agent/providers.py`, where the
  `anthropic_messages` route serves Claude on signature-dropping gateways. A
  caller-named LLM destination does NOT get the server's credential:
  `core/llm_provider_utils.py` owns that predicate and it is enforced at
  consumption, because the per-thread config file is writable by file tools.
- Reasoning effort: levels `off` through `max` in `config/model_capabilities.py`,
  clamped once per thread at `core/agent_llm_config.py`. "off" is a persisted
  winning value that turning thinking back on must clear (`/think on`; the CLI
  exposes it as `/reasoning`).
- Compaction triggers on absolute tokens (default 200k, clamped to model
  context) at sub-turn boundaries; `/prune` compresses tool results
  deterministically; fresh threads seed a memory-load turn. Doc:
  `Nymeria/docs/agent-systems/compaction-and-checkpoints.md`.
- SSE event types are normalized in `triggers/sse_consumer.py` and documented in
  `Nymeria/docs/api.md`; chat streams carry keepalive comment frames
  (`api/sse.py`).
- Every holder turn is re-attachable and watchable live: the holder tees
  seq-stamped payloads into `core/turn_stream_buffer.py`, and
  `GET /threads/{id}/turn/stream` replays byte-identically then tails. That
  attach is the only GUI renderer for autonomous turns.
- Iteration-limit halts are graceful and resumable: the cap is enforced in
  `route_after_tools` (post-batch), not in `should_continue` (which keeps the
  repeated-tool guard), so a halt leaves a clean ToolMessage-terminal tail that
  `/resume` re-drives.
- Cancellation enters at `POST /threads/{id}/stop`
  (`api/routers/thread_operations.py`) and cascades through
  `NymeriaAgent.abort_with_cascade()`.
- Provider refusals and model switching are one subsystem. A pre-output refusal
  is HTTP 200 with no text and no tool calls, not an exception, so the
  retry and fallback machinery never sees it: the node swaps to the next
  fallback candidate, or `agent_context.maybe_rewind_refused_turn` rewinds.
  401, 403, and 400 fall back immediately and only cross-route; retryables keep
  the full chain. Consent crosses the vendored boundary only as
  `LLMConfig.fallback_decision_callback` (`core/fallback_approvals.py`).
- Settings source of truth is `config/settings.py`; HTTP updates go through
  `PATCH /settings`; dotenv writes share `config/env_file.py` with the wizard.
  A booted process never re-reads its env files, so applying a file edit is
  explicit: `POST /settings/reload` (transactional, invalid values roll back)
  or a restart.
- Package `__init__.py` re-exports are LAZY (PEP 562) in `nymeria/__init__.py`,
  `core/`, `triggers/`, and `vendor/react_agent/`. This is load-bearing: one
  eager import in `triggers/__init__.py` once cost 14.9s of a 16.4s thin-CLI
  launch. Do not add a module-level import of a heavy sibling to these inits,
  and do not tidy a function-local import in `triggers/cli/` up to module
  scope. `Nymeria/tests/test_cli_startup_imports.py` gates it.

## Tools and Skills Co-Design

One rule family for anyone touching `Nymeria/nymeria/tools/`,
`Nymeria/nymeria/skills/`, or `Nymeria/nymeria/skills_bundled/`:

1. A tool's own description must STAND ALONE. Never thin or strip a tool
   description (or its argument descriptions) because a kit's SKILL.md
   "already covers it". A kit body enters context only when the skill is
   activated; the tool binds and runs through many pathways that never load
   that text: promotion into `default_thread_tools`, per-thread
   `enabled_tools`, `tool_manage` enable, `tool_search` plus bind, one-shot
   `tool_invoke` calls, `spawn_thread` tool queries, another kit's
   `required_tools`, and workflow (`nym.*`) calls. An agent on any of those
   paths has only the tool schema to go on.
2. The split: the tool description carries the per-tool contract (what it does,
   arguments, failure modes, cost and risk notes); the kit's SKILL.md carries
   cross-tool PROCEDURE (which tool when, ordering, recipes) and workflow-level
   framing. Overlap is fine; dependence is not. The test: deleting every
   SKILL.md must leave every tool fully usable from its schema alone.
3. A safety rule that gates an action lives in the tool's description or its
   own code, never only in a kit body.

## Test Suite

`Nymeria/tests/` is the highest-churn directory in the repo. Rule 3 above is the
writing standard; these are the mechanics.

- `Nymeria/tests/conftest.py` is load-bearing hermeticity in three layers: the
  project-root pin above the first `nymeria` import, the dotenv chain emptied,
  and `os.environ` snapshotted pre-collection and restored per test. Read its
  comments before touching fixtures, and never import `nymeria.*` above the pin.
- Several files are build-failing gates or ratchets rather than coverage
  (`test_subprocess_env_gate.py`, `test_exec_sandbox_gate.py`,
  `test_service_integration_egress.py`, `test_cli_startup_imports.py`,
  `test_command_doc_coverage.py`, `test_resource_layout.py`, more). Weakening
  one is a design change, not a test fix.
- Flat directory, 500+ files, prefix-descriptive naming: prefix-grep for a
  subject's existing tests before creating a new file.
- The full suite takes roughly 9 minutes. Iterate on the relevant files only,
  `--lf` works, and read the "N passed" line rather than the exit code: a
  mistyped path under `-n` exits 0 having run nothing.

## Frontend Code Map

Read `Nymeria/docs/frontends/desktop-vs-mobile.md` before changing files shared
by both clients.

**Desktop** (`nymeria-desktop/`, Svelte 5 + Tauri 2):

| Path | Purpose |
| --- | --- |
| `src/lib/stores/` | ~35 runes stores; desktop-only: authPrompt, backendProcess, browserLogin, cliproxy, connections, onboarding, outlook, profilePics, syncPoll, uiPrompt |
| `src/lib/components/` | By domain: onboarding (first-run setup hub with wizard parity over `GET`/`PATCH /settings`), chat (messages, input, streaming, thinking blocks, tool cards), threads (list plus per-thread config tabs: Agent, Behavior, Connections, Dreaming, Hooks, Memory, Model, Skills, Tools), account, tools (tool and MCP management), dashboard, todos, common, layout, credentials, skills, notifications, triggers, hooks (feed, adaptive form modal, per-hook execution log), workflows (approval rows plus live and recent runs), artifacts (sandboxed-iframe `ui_prompt` renderer, desktop-only), browser (live login-handoff viewer, desktop-only), outlook, dev |
| `src/lib/services/api/` | Typed API client inheritance chain ending at `NymeriaAPI` in `index.ts`; `humanizeError.ts` is the canonical error-copy layer |
| `src/lib/themes.ts`, `src/lib/utils/transitions.ts`, `src/lib/services/api.svelte.ts` | Content-identical across desktop and mobile; three of the enforced EXACT_MATCH files (authoritative list in `scripts/check_cross_app_drift.py`). `transitions.ts` is the single source for JS motion constants |
| `src/app.css` | Per-app global CSS: motion and easing tokens, global keyframes, reduced-motion floor |
| `src/lib/types/`, `src/lib/utils/`, `src/lib/actions/` | Shared types; markdown, file, id, model, and tool helpers; Svelte actions |
| `src-tauri/` | Rust and Tauri 2 shell: commands, `process_manager.rs` (local backend), tray, capabilities, icons |

**Mobile** (`nymeria-mobile/`, Svelte 5 + Capacitor 6): not actively worked on
and never reached a finished state, but cross-app changes are still mirrored
into it so a future finishing pass starts from parity. Mirrors desktop
`src/lib` minus the dev, outlook, skills, todos, onboarding, and artifacts
component dirs (todo UI lives in its `components/dashboard/`); no per-thread
config tab components (thread config lives in its ThreadSettingsPanel); shell
is MobileShell, LeftPanel, ChatPanel, RightPanel; keeps its own `services/api/`
copy; adds haptics, lifecycle, providerGroups, and threadPlatform utils.

Sharp edge: `scripts/check_cross_app_drift.py` discovers `.ts`, `.svelte`, and
`.css` files shared by both apps and classifies them via its in-script
EXACT_MATCH (content-identical, CRLF-normalized) and KNOWN_DRIFT sets. Any new
shared file of those types must be added to one set or the check fails.

The web UI the API serves is built from the desktop client:
`scripts/build_frontend.sh` rebuilds it and copies the output into
`Nymeria/nymeria/frontend/`, which the release workflow does on every release.
Treat that directory as generated.

## Documentation Map

Canonical docs live in `Nymeria/docs/`. This is a MAP: it says what each doc IS
and where it lives, deliberately not what it says. Many docs lag the code, so
read them as context to check against the code, and fix errors you spot. Paths
below are relative to `Nymeria/docs/`.

- Start here: `getting-started/PROJECT_BRIEF.md`, `getting-started/architecture.md`
  (includes the RAG section), `getting-started/QUICKSTART.md`,
  `getting-started/feature-list.md`, `getting-started/ROADMAP.md`, plus the
  cross-cutting `api.md` (REST and SSE) and `configuration.md` (all env vars).
- Agent systems, one doc per subsystem, all in `agent-systems/`: `tools.md`,
  `tools-index.md` (GENERATED; its header carries the regen command and scope
  caveats), `tool-hot-loading.md`, `skills.md`, `hooks.md`, `dreaming.md`,
  `triggers.md`, `credentials.md`, `accounts.md`,
  `compaction-and-checkpoints.md`, `memory-and-compaction-rationale.md`,
  `resource-filesystem.md`, `reasoning-streaming.md`, `notifications.md`,
  `logging.md`, `user-todo-management.md`, and `claude-code-bridge.md` (the
  agent driving Claude Code on the host, which the filename does not give away).
- Deployment, all in `deployment/`: start at `deployment-README.md` (chooser);
  siblings cover shapes, slim, remote access, production, backup and restore
  (the `snapshot` CLI), and the release workflow.
- Providers: `providers/openrouter.md`, `providers/local-llm.md`.
- Frontends, all in `frontends/`: `web-client.md`, `desktop-vs-mobile.md`,
  `desktop-client-only.md`, `frontend-accounts.md`, and
  `per-thread-backend-connections.md` (design only, not implemented).
- Chat-platform bots: `chat-apps/<platform>-bot.md`, one each for discord,
  telegram, slack, teams, twitch, and whatsapp.

## CLIProxy Warning

CLIProxy is an optional gateway that fronts subscription-OAuth model access.
Its config is delicate: the working setup was found by trial and error, and a
wrong one can cause 429 errors across every model, so change it deliberately.
The proxy is pulled as a Docker image; the config template lives at
`CLIProxyAPI-main/temp/latest/config.yaml.example`.

- The CLIProxy treatment is three parts, all keyed off
  `looks_like_cliproxy_url()` in `nymeria/vendor/react_agent/cliproxy.py`: the
  cloak-skip User-Agent and an `Anthropic-Beta` header override, both applied
  in `providers.py::_create_anthropic_llm`, plus a billing-fingerprint system
  block injected on every request payload by the factory's CLIProxy branch
  (zero client-side `cache_control` there is load-bearing). Smoke test after
  any LLM-stack change: `Nymeria/tools/check_cliproxy_cloak.py`.
- The proxy returns `context_management` as a plain dict; an adapter in
  `providers.py` wraps it for langchain-anthropic.
- Management integration (subscription OAuth, opt-in): catalog and HTTP client
  in `nymeria/cliproxy/`, admin REST surface in `api/routers/cliproxy.py`
  (`POST /cliproxy/apply-route` is the route-shape truth, and
  `POST /cliproxy/verify` is the only check proving a credential serves
  traffic), wizard branch in `setup/steps/cliproxy.py`, headless branch in
  `setup/cliproxy_login.py`. Gated by `CLIPROXY_MANAGEMENT_URL` and
  `CLIPROXY_MANAGEMENT_KEY`.

## MCP Server

```bash
python3 run.py mcp                 # STDIO (local, trusted; ungated)
python3 run.py mcp --http --port 8001
```

`nymeria/mcp_server.py` is a thin client over the API: no agent, checkpointer,
TODO manager, or profile state. It exposes 54 MCP tools (may have changed)
across health and auth, chat, background chat, slash commands, triggers,
threads, thread config, global settings, TODOs, profile, memory, RAG, and
notification routing. Chat is bounded and resumable, and its wait must stay
under the CLIENT's transport timeout or the partial-return branch is dead code.

It authenticates with `NYMERIA_SERVICE_TOKEN` and uses `X-Nymeria-Act-As` for
user-scoped calls. In HTTP mode `MCPAuthMiddleware` (`nymeria/mcp_auth.py`)
requires an inbound `Authorization: Bearer <account token>` resolved via the
backend `/me`; that identity governs Act-As, so non-admins are pinned to their
own `user_id` (any `user_id` argument is ignored) and admins keep Act-As.
`NYMERIA_MCP_ALLOW_UNAUTHENTICATED=1` disables the gate; never set it on a
reachable endpoint, and never expose the endpoint beyond loopback or a private
mesh.
