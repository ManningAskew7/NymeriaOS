# NymeriaOS

A self-hosted personal AI assistant platform: a Python agent backend you run
on your own machine or server, with a web UI, a Windows desktop app, and
chat-platform bots (Discord, Telegram, Slack, Twitch, WhatsApp, Teams) as
front doors. Every conversation thread is its own configurable agent (its
model, tools, skills, memory, hooks, and schedule), threads can call each
other as tools, and the agent can extend the platform at runtime with new
tools, skills, workflows, and triggers.

**Status: public beta (pre-1.0).** It works and it is used daily, but expect
rough edges, breaking changes between betas, and docs that occasionally lag
the code. Please file what you hit as a GitHub issue.

**Beta scope: one person per deployment.** The account model supports several
users on one backend, but multi-user operation is not yet tested or supported
in the beta. Run a separate deployment for each person and use the owner
(admin) account that `nymeria init` creates on first run; treat additional
accounts as experimental.

## Install

You need Python 3.11+ (the `uv` installer can fetch one for you) and an LLM
provider: an API key (Anthropic, OpenAI, OpenRouter, Google, and many more),
an existing AI subscription through the optional CLIProxy gateway, or a local
model via Ollama. The setup wizard walks you through all three.

**Stable (recommended):** the released package from PyPI.

```bash
uv tool install nymeriaos     # or: pipx install nymeriaos
nymeria init                  # guided setup: hosting, LLM, timezone
nymeria doctor                # check the install
nymeria slim                  # API + scheduler + MCP in one process; open http://localhost:8000
```

Upgrade later with `uv tool upgrade nymeriaos`. To try it without installing:
`uvx --from nymeriaos nymeria slim`.

**One-line installer** (Linux, macOS, WSL; asks which track you want):

```bash
curl -fsSL https://raw.githubusercontent.com/ManningAskew7/NymeriaOS/main/install.sh | sh
```

**Windows** (PowerShell; installs the stable track and can start the backend
at logon):

```powershell
irm https://raw.githubusercontent.com/ManningAskew7/NymeriaOS/main/install.ps1 | iex
```

**Source** (hackable: edits under the checkout apply on the next restart,
which is also what lets the agent modify its own source):

```bash
git clone https://github.com/ManningAskew7/NymeriaOS.git ~/NymeriaOS
uv tool install --editable ~/NymeriaOS/Nymeria
nymeria init
```

Update a source install with `git -C ~/NymeriaOS pull --ff-only` (re-run the
`uv tool install` line only when dependencies changed).

**Docker** needs the source install above (a `nymeria` installed from PyPI
stays clone-free even when run inside a checkout, so the wizard greys Docker
out there). From `~/NymeriaOS/Nymeria`, create the env file first, then either
let the wizard run the single-container shape for you or start it by hand:

```bash
cp .env.docker.example .env.docker      # then set the passwords it asks for
docker compose -f docker-compose.single.yml up -d --build
```

The full Postgres + Redis stack is
`docker compose --env-file .env.docker up -d --build` in the same directory
(`.env.docker` must set `POSTGRES_PASSWORD` and `REDIS_PASSWORD`). No
container images are published during the beta; both shapes build locally.

Optional extras install as `uv tool install "nymeriaos[discord]"` (also
`telegram`, `slack`, `bots`, `postgres`, `redis`, `voice`, `browser`,
`firebase`, `all`).

## Desktop app

The Windows desktop app is attached to each
[release](https://github.com/ManningAskew7/NymeriaOS/releases) as an
installer. It is a client: point it at any backend URL (`http://localhost:8000`
for a local install) and sign in with the token the wizard prints. The
installer is not code-signed during the beta, so Windows SmartScreen will warn
once ("More info", then "Run anyway"). macOS and Linux users use the web UI
the backend serves.

## Documentation

- [Quick start](Nymeria/docs/getting-started/QUICKSTART.md), then the
  [project brief](Nymeria/docs/getting-started/PROJECT_BRIEF.md),
  [architecture](Nymeria/docs/getting-started/architecture.md), and
  the [feature list](Nymeria/docs/getting-started/feature-list.md).
- [Deployment shapes](Nymeria/docs/deployment/deployment-README.md)
  (single process, single container, full stack, remote access, backups).
- [REST and SSE API](Nymeria/docs/api.md) and every
  [configuration variable](Nymeria/docs/configuration.md).
- Agent systems: [tools](Nymeria/docs/agent-systems/tools.md),
  [skills and kits](Nymeria/docs/agent-systems/skills.md),
  [hooks](Nymeria/docs/agent-systems/hooks.md),
  [triggers](Nymeria/docs/agent-systems/triggers.md),
  [accounts](Nymeria/docs/agent-systems/accounts.md), and the rest of
  [`agent-systems/`](Nymeria/docs/agent-systems/).
- Clients: [web UI](Nymeria/docs/frontends/web-client.md),
  [desktop](Nymeria/docs/frontends/desktop-client-only.md), and the
  [chat-platform bots](Nymeria/docs/chat-apps/).
- Working on the code: `CLAUDE.md` (the code map, also for coding agents),
  `Nymeria/docs/deployment/release-workflow.md`.

## Security and privacy

Self-hosted and local-first: your data stays in your install's data
directory, and nothing phones home (no telemetry, no crash reporting, no
update checks). The trust model, including what the agent can and cannot be
prevented from doing on the machine it runs on, is in [SECURITY.md](SECURITY.md).
Read it before exposing an install beyond localhost.

## License

[PolyForm Noncommercial 1.0.0](LICENSE): free for personal, hobby, research,
and other noncommercial use. Third-party components are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
