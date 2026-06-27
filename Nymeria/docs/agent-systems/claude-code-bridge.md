# Claude Code bridge (`claude_code` tool)

The `claude_code` tool lets Nymeria drive Claude Code (Anthropic's CLI coding
agent) to do real work in a project: substantive coding, refactors, multi-file
edits, writing to project docs, or any task best handed to a dedicated coding
agent with file and shell access. Nymeria sends a prompt, Claude Code runs where
the repo lives, and Nymeria relays the final message plus a summary of what
changed back to the user (e.g. over Telegram).

It is admin-only, off by default (`CATALOG_TOOLS`), and rated `SENSITIVE`.

## Why a bridge

Nymeria runs in a read-only Docker container that mounts only `nymeria/`,
`tests/`, and `run.py` (read-only) plus writable named volumes. It cannot see the
host repo, and the container's `claude` binary (if present) is authenticated with
CLIProxy keys, not real Anthropic auth. So Claude Code must run on the **host**,
where the repo, the `claude` binary, and real auth live.

Two components, selected by environment:

```
Telegram -> Nymeria agent (container) --claude_code tool-->  [HTTP + bearer token]
                                                  -> Host runner (python run.py claude-code-runner)
                                                     -> claude -p --output-format json
                                                  <- final text + run summary (+ session id)
   <- Nymeria relays the result    (long/detached runs arrive as a follow-up autonomous message)
```

- **Container-side tool** (`nymeria/tools/claude_code.py`): the agent's interface.
- **Host runner** (`nymeria/gateway/claude_code_runner.py`): a small FastAPI
  service that executes Claude Code on the host. It is the **policy boundary**: it
  independently requires the bearer token, re-resolves the working-directory
  allowlist, re-maps the permission mode, applies the hard deny rules, and builds
  the run config from its own host environment. A compromised container cannot
  make it step outside the allowlist or deny rules.

Shared logic lives in `nymeria/tools/claude_code_bridge.py` so the tool and the
runner build and interpret identical Claude Code invocations.

### Transport selection

- `NYMERIA_CLAUDE_CODE_URL` **set**: remote mode. The tool POSTs the run to the
  host runner and polls it to completion. This is the production Docker path.
- `NYMERIA_CLAUDE_CODE_URL` **unset**: local mode. The tool runs `claude -p` in
  process. This is the slim / desktop path where the agent and `claude` are
  co-located.

## Long runs: block, then detach

A Claude Code run can outlast the runtime's per-tool-call timeout
(`settings.tool_timeout`, hard-capped at 900s), which would orphan the work. So
every run is driven by a background watcher and the tool only *waits* on it for a
bounded budget (kept below `tool_timeout`):

- finishes within budget -> result returns inline;
- exceeds budget (or `detach=True`) -> the tool returns a "working in the
  background" notice and the watcher delivers an autonomous completion turn (the
  same path as background bash: a pending prompt if the thread is busy, otherwise
  a self-invoked turn with SSE `task_started` / `task_completed`).

The inline-vs-detach decision resolves exactly once, so a run is never notified
twice and never silently dropped, even if the runtime kills the waiting tool
thread.

## Permission modes

The per-call `mode` maps to Claude Code's `--permission-mode`:

| `mode` (friendly)     | Claude Code mode     | Behavior |
| --- | --- | --- |
| `dont_ask` (default)  | `dontAsk`            | Deny-by-default, never prompts, allowlist-scoped. |
| `plan`                | `plan`              | Writes an implementation plan and STOPS without editing. Nymeria reads the plan, then calls again to execute. |
| `accept_edits`        | `acceptEdits`       | Auto-accepts file edits. |
| `auto`                | `auto`              | Autonomy classifier decides per action. |
| `bypass`              | `bypassPermissions` | Full autonomy / oneshot. |
| `default`             | `default`           | Standard interactive prompting (rarely useful headless). |

Hard deny rules (`NYMERIA_CLAUDE_CODE_DISALLOWED_TOOLS`, default: `rm`, `rmdir`,
`sudo`, `git push`, `git reset`, `git clean`, `shutdown`, `reboot`, `mkfs`, `dd`)
are passed as `--disallowedTools` and enforced in **every** mode, including
`bypass`. They are command-prefix guardrails against accidental destructive
commands, not a sandbox: an adversarial prompt can route around a prefix match
(`find -delete`, `python -c`, ...). The real containment boundary is the
working-directory allowlist plus running the runner as an unprivileged user on an
isolated checkout (and Claude Code's own OS sandbox if you enable it).

## Sessions

Claude Code sessions are cwd-scoped on the host. The tool persists a
`(thread_id, project) -> session_id` map (`data/claude_code_sessions.json`) so
`resume=True` continues the same conversation in the same directory across turns.

## Auth

Default: Claude Code's own OAuth / keychain auth on the host (zero added cost when
backed by a subscription). The bridge strips any `ANTHROPIC_API_KEY` /
`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` the Nymeria process carries (e.g. a
CLIProxy `cpx-` key) before invoking `claude`, so it falls back to that host auth.

Alternative: set `NYMERIA_CLAUDE_CODE_BARE=true` with a real Console
`ANTHROPIC_API_KEY` for an isolated, metered run (`--bare` also skips host hooks
and `CLAUDE.md` auto-discovery; it forces API-key auth, OAuth/keychain are never
read).

## Configuration

| Env var | Purpose |
| --- | --- |
| `NYMERIA_CLAUDE_CODE_URL` | Host runner base URL. Unset = local mode. |
| `NYMERIA_CLAUDE_CODE_TOKEN` | Bearer token shared by the tool and runner. |
| `NYMERIA_CLAUDE_CODE_ROOTS` | Allowed working-directory roots (os.pathsep or comma separated). Empty = project root only. |
| `NYMERIA_CLAUDE_CODE_MODEL` | Model alias/id Claude Code runs with (e.g. `opus`). |
| `NYMERIA_CLAUDE_CODE_FALLBACK_MODEL` | Fallback model. |
| `NYMERIA_CLAUDE_CODE_MAX_TURNS` | Cap on Claude Code turns per run. |
| `NYMERIA_CLAUDE_CODE_MAX_BUDGET_USD` | Per-run USD budget cap. |
| `NYMERIA_CLAUDE_CODE_DISALLOWED_TOOLS` | Override the hard deny list. |
| `NYMERIA_CLAUDE_CODE_BARE` | Run with `--bare` (API-key auth). |
| `NYMERIA_CLAUDE_CODE_DEFAULT_MODE` | Default permission mode (default `dontAsk`). |
| `NYMERIA_CLAUDE_CODE_BLOCK_SECONDS` | Max inline wait before detaching. None = derive from `tool_timeout`. |

## Running the host runner (production / Docker)

Run the runner on the host (where the repo, the `claude` binary, and real auth
live), as an **unprivileged** user (never root). The `api` service in
`docker-compose.yml` already ships the wiring (`extra_hosts:
host.docker.internal:host-gateway` and the `NYMERIA_CLAUDE_CODE_*` env keys), so
production deployment is: set three values in `.env.docker`, open the port to the
Docker bridge, and run the runner as a service.

### 1. Set three env vars in `.env.docker`

```bash
NYMERIA_CLAUDE_CODE_URL=http://host.docker.internal:8200
NYMERIA_CLAUDE_CODE_TOKEN=<openssl rand -hex 32>
NYMERIA_CLAUDE_CODE_ROOTS=/opt/NymeriaOS
```

Recreate the api container so it picks them up: `docker compose --env-file
.env.docker up -d api` (recreate, **not** `restart`).

### 2. Open port 8200 to the Docker bridge ONLY (required)

The container reaches the host at `host.docker.internal`, which Docker's
`host-gateway` resolves to the docker0 gateway `172.17.0.1`. With a default-deny
firewall (`ufw` `DEFAULT_INPUT_POLICY="DROP"`), traffic from the Docker bridges to
a host port is dropped, so the container **cannot reach the runner** until you
allow Docker's private range to that port:

```bash
sudo ufw allow from 172.16.0.0/12 to any port 8200 proto tcp \
  comment 'nymeria claude-code runner (docker bridge only)'
```

This allows only Docker's private range (`172.16.0.0/12`), never the public
internet (UFW still denies 8200 from anywhere else). The bearer token is the auth
boundary regardless.

### 3. Run the runner as a systemd user service (durable)

`nymeria service` only manages the slim backend, so install a small unit by hand at
`~/.config/systemd/user/nymeria-claude-code-runner.service` (replace `<user>`):

```ini
[Unit]
Description=Nymeria Claude Code runner (claude_code tool host bridge)
After=default.target

[Service]
Type=simple
WorkingDirectory=/opt/NymeriaOS/Nymeria
Environment=HOME=/home/<user>
Environment=PATH=/home/<user>/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=NYMERIA_CLAUDE_CODE_TOKEN=<same token as .env.docker>
Environment=NYMERIA_CLAUDE_CODE_ROOTS=/opt/NymeriaOS
ExecStart=/usr/bin/python3 run.py claude-code-runner --host 172.17.0.1 --port 8200
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
chmod 600 ~/.config/systemd/user/nymeria-claude-code-runner.service
systemctl --user daemon-reload
systemctl --user enable --now nymeria-claude-code-runner.service
loginctl enable-linger "$USER"   # survive logout / reboot
```

`PATH` must include the dir holding `claude` (so the runner can spawn it) and
`git` (for the change summary). Bind `172.17.0.1` (the private docker0 gateway),
not `0.0.0.0`. The runner needs no Nymeria DB or service token.

### Verify

```bash
curl -s http://172.17.0.1:8200/health                          # host
docker exec nymeria-api curl -s http://host.docker.internal:8200/health  # container
# both -> {"status":"ok","claude":true}
```

A `claude:true` health plus a real run (a "reply PONG" prompt) confirms the host
`claude` is authenticated (the runner sets no `ANTHROPIC_API_KEY`, so non-bare runs
fall through to the host's OAuth / keychain auth).

### Security

- The runner is an RCE-capable endpoint. Keep it private: bind `172.17.0.1` and
  scope the UFW rule to the Docker range as above; never expose 8200 publicly. Run
  as an unprivileged user, never root.
- Always set `NYMERIA_CLAUDE_CODE_TOKEN`; `--insecure` (tokenless) is loopback dev
  only.
- Keep `NYMERIA_CLAUDE_CODE_ROOTS` tight; it is the directory sandbox.
- A future option for zero network exposure is a Unix-domain-socket transport (no
  open port); the firewalled-TCP + token setup above is the current shape.

## Usage examples

```text
# Add an idea to the backlog with context (the first use case)
claude_code("Add a backlog item under docs/private/plans/backlog for <idea>, "
            "with the usual Interpretation/Surface/Implications fields.",
            mode="bypass")

# Plan first, confirm, then execute
claude_code("Refactor the X module to do Y", mode="plan")     # returns a plan
claude_code("Looks good, implement it", mode="accept_edits")  # resume + execute

# Fire and continue talking
claude_code("Run the full test suite and fix any failures", detach=True)
```
