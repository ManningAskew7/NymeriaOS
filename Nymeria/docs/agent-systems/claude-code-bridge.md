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
                                                     -> claude -p --output-format stream-json --verbose
                                                  <- session id, every end-turn as it lands, transcript tail
                                                  <- final text + run summary
   <- Nymeria relays each report   (every end-turn is its own follow-up message, tagged job + session)
```

- **Container-side tool** (`nymeria/tools/claude_code.py`): the agent's interface.
- **Host runner** (`nymeria/gateway/claude_code_runner.py`): a small FastAPI
  service that executes Claude Code on the host. It is the **policy boundary**: it
  independently requires the bearer token, re-resolves the working-directory
  allowlist, re-maps the permission mode, applies the hard deny rules, and builds
  the run config from its own host environment. A compromised container cannot
  make it step outside the allowlist or deny rules. Endpoints: `POST /run`,
  `GET /job/{id}` (`status`, `session_id`, `end_turns` so far, `result` when
  done), `GET /job/{id}/peek?tail=N`, `GET /sessions/{session_id}/peek`,
  `POST /cancel/{id}`, `GET /health`. The runner is a long-lived process
  loaded from the checkout: restart the service after updating the code
  (`systemctl --user restart nymeria-claude-code-runner`), at a quiet moment,
  since the restart kills its in-flight runs. The tool degrades against an
  older runner (session and end-turns known only at completion, no peek).

Shared logic lives in `nymeria/tools/claude_code_bridge.py` so the tool and the
runner build and interpret identical Claude Code invocations.

### Transport selection

- `NYMERIA_CLAUDE_CODE_URL` **set**: remote mode. The tool POSTs the run to the
  host runner and polls it to completion. This is the production Docker path.
- `NYMERIA_CLAUDE_CODE_URL` **unset**: local mode. The tool runs `claude -p` in
  process. This is the slim / desktop path where the agent and `claude` are
  co-located.

## Reports: every end-turn, block then detach

Claude Code is driven with `--output-format stream-json`, one JSON event per
line: a `system/init` event naming the session id up front, `assistant` and
`user` events per content block, and one `result` event PER END-TURN. A `-p`
run normally ends one turn and exits a few seconds later, but a run that
ended its turn with background subagents outstanding is re-invoked when they
report and ends another turn in the same process (a background Bash task does
NOT keep it alive: the CLI kills those at exit). The old bridge parsed only the
process's terminal object, so a session that said "suite running, will pick
up" as an early end-turn delivered THAT as its completion and everything after
had no path back (job 3c35ee19, 2026-09-04).

Now `RunObserver` (`claude_code_bridge.py`) folds the stream into the session
id, the list of end-turns and a bounded transcript tail, and the watcher
(`claude_code_background.py`) turns each end-turn into a REPORT delivered to
the thread as its own completion prompt:

- **Tagged.** Every report opens with
  `[Claude Code job <id> | session <id> | INTERIM end-turn k ...]` or
  `[... | FINAL: run finished after Ns with N end-turn(s) ...]`, and every
  tool return, `[Queued]` receipt and peek carries the same job + session tag,
  so the agent always knows which run is talking and how to address it
  (`resume="<session id>"`, `peek="<job id>"`).
- **Interim vs final.** An observed end-turn is held `END_TURN_SETTLE_SECONDS`
  (30s) for the process to exit; the common single-turn run merges into one
  FINAL report. A turn the process outlives goes out as INTERIM ("the run is
  still going; more follows"), with its own task id
  (`claude-code-<job>-t<k>`) and activity entry. The FINAL report carries the
  run summary (files changed, commits, cost, duration, `end_turns` count); if
  its last end-turn was already delivered it says so instead of repeating it.
- **Block, then detach.** A run can outlast the runtime's per-tool-call
  timeout (`settings.tool_timeout`, hard-capped at 900s), so the tool only
  *waits* for the FIRST report, up to a budget kept below `tool_timeout`
  (`NYMERIA_CLAUDE_CODE_BLOCK_SECONDS` overrides). Ready in time: it returns
  inline (an INTERIM first report is a valid inline answer; the rest follow).
  Not in time, or `detach=True`: the tool returns a tagged "working in the
  background" notice and every report arrives as an autonomous completion
  turn (the same path as background bash: a pending prompt if the thread is
  busy, otherwise a self-invoked turn with SSE `task_started` /
  `task_completed`). The first report's inline-or-detached decision resolves
  exactly once (`InlineLatch`), so it is never delivered twice and never
  dropped; later reports are always delivered.
- **Cancellation.** A `/stop` cancels the run; its FINAL report stays silent
  (an interim already delivered is real output and stands).

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

Nymeria's own Landlock sandbox (`EXEC_SANDBOX_ENABLED`) does not help here, and
this is the one surface in the codebase where it cannot: Claude Code aborts on
`SIGABRT` with no output under the `/proc` denial the policy always applies,
measured against the real binary. So the bridge is the single declared exemption
from that control. The `git` before/after summary the bridge runs IS confined,
which closes one route (a planted `core.fsmonitor` in the target repository can
no longer read the host's process environment through `git status`), but it does
not bound the run itself. Treat a Claude Code run as having the reach of the
account the runner runs as.

## Sessions

Claude Code sessions are cwd-scoped on the host. The tool persists a
`(thread_id, project) -> session_id` map (`data/claude_code_sessions.json`),
written on FIRST SIGHT of the session id (the `init` event, or the runner's
first poll that carries it), not at the end of the run, so the thread's "last
session" is the one in flight.

`resume` is addressable:

- `True` (default): the thread's stored session for that directory.
- A **session id**, or a **bridge job id** from any report: that specific
  session, whichever thread or run last used it. This is how a follow-up
  reaches the run you mean when several are in flight (the 2026-09-04
  incidents: `resume=True` with a detached job running resumed the previous
  session instead).
- `False`: a fresh session.

A prompt for a session that is **still running** cannot be injected into the
`-p` process, and resuming it concurrently would fork the conversation. It is
QUEUED on the live job instead (`ClaudeCodeJob.followups`) and the tool
returns a `[Queued]` receipt at once, whatever `detach` says; when the run
ends, the queued prompt(s) start as a new job resuming that session (same
thread, mode, cwd and deliverer), and its reports arrive tagged with the same
session id and the new job id. This is the callable-thread "follow-up wake"
shape: a receipt now, the answer later, no polling. Bare `resume=True` queues
the same way when the stored session is the live one. A run cancelled by
`/stop` drops its queued follow-ups.

## Peek: look at a running session without resuming it

`claude_code(peek="<job id> | <session id> | latest", tail=N)` returns the
session's live state: RUNNING or finished, elapsed time, its end-turns so
far, and the last N transcript entries (assistant text, tool calls with a
compact input preview, tool results, task notifications), each timestamped.
Read-only: nothing is resumed and no Claude Code turn is spent. In local mode
it reads the in-process `RunObserver`; in remote mode it calls the runner's
`GET /job/{id}/peek?tail=N` (the runner also answers
`GET /sessions/{session_id}/peek`). The runner keeps up to 200 entries per
job; jobs stay addressable in the tool's live registry after they finish (the
64 most recent).

## Bridge context: every prompt names its thread

Every prompt the bridge sends (tool and `/code` alike) is prefixed with a
`[Nymeria bridge context] ... [end bridge context]` block
(`claude_code_bridge.frame_prompt`) naming the originating Nymeria thread
id, the bridge job id and, when resuming, the session id. It tells Claude
Code that its end-turn messages reach that thread automatically (tagged, so
it need not repeat them) and how to message the thread itself mid-task:
the Nymeria MCP tool `nymeria_chat` with that `thread_id` when the MCP
server is available to it, else `POST /threads/{id}/chat` on the API with a
bearer token, opening the message with `[Claude Code job <id>]` so the thread
knows who is talking. The user's raw prompt follows the block unchanged; a
run with no thread context (direct CLI, tests) goes out bare.

## Auth

Default: Claude Code's own OAuth / keychain auth on the host (zero added cost when
backed by a subscription). The child's environment is an **allowlist**
(`build_subprocess_env`), not the parent's environment with a few names removed, so
`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` are simply never
present on this path (no CLIProxy `cpx-` key leaks in) and Claude Code falls back to host
auth. The allowlist is network/TLS settings, a short list of runtime names (`SHELL`,
`TERM`, `SSH_AUTH_SOCK`, `GIT_CONFIG_GLOBAL`, and similar), and everything matching the
`CLAUDE_` prefix. If Claude Code misbehaves in a way that tracks a host setting, add the
name to `_CLAUDE_CODE_RUNTIME_PASSTHROUGH` rather than restoring a full copy: the child
is itself an agent that reads its own environment, and it used to receive Nymeria's
master encryption key, service token and database credentials.

Alternative: set `NYMERIA_CLAUDE_CODE_BARE=true` with a real Console
`ANTHROPIC_API_KEY` for an isolated, metered run. Bare mode is the only path that ADDS
the three `ANTHROPIC_*` names to the allowlist, because Claude Code needs a key there
(`--bare` also skips host hooks and `CLAUDE.md` auto-discovery; it forces API-key auth,
OAuth/keychain are never read).

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
| `NYMERIA_CLAUDE_CODE_MAX_CONCURRENCY` | Max concurrent runs the host runner executes at once (default `2`). Bounds host memory under bursts. |
| `NYMERIA_CLAUDE_CODE_ALLOWED_MODELS` | Allowlist of models a per-thread override may request on the runner (comma/os.pathsep). Empty = accept any. |
| `NYMERIA_CLAUDE_CODE_DISALLOWED_TOOLS` | Override the hard deny list. |
| `NYMERIA_CLAUDE_CODE_BARE` | Run with `--bare` (API-key auth). |
| `NYMERIA_CLAUDE_CODE_DEFAULT_MODE` | Default permission mode (default `dontAsk`). |
| `NYMERIA_CLAUDE_CODE_BLOCK_SECONDS` | Max inline wait before detaching. None = derive from `tool_timeout`. |

### Per-thread overrides

A thread can override the model and the default permission mode for `claude_code`
without touching global env, via `ThreadConfig.claude_code_model` and
`ThreadConfig.claude_code_mode` (PATCH `/threads/{id}/config`). Precedence for the
mode is **per-call `mode` argument > per-thread `claude_code_mode` > global
`NYMERIA_CLAUDE_CODE_DEFAULT_MODE`**; the model override falls back to
`NYMERIA_CLAUDE_CODE_MODEL` when unset. In remote mode the requested model travels
in the `POST /run` body and the runner honors it only if it passes
`NYMERIA_CLAUDE_CODE_ALLOWED_MODELS` (unset = accept any); the runner stays the
policy authority, and budget caps plus the cwd allowlist remain the real boundary.

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

## `/code`: the user drives Claude Code directly (break-glass)

The `claude_code` tool is the AGENT's way in. `/code <prompt>` is the USER's:
an admin-only slash command that dispatches the prompt to the same host runner
(or local `claude -p`) with NO model in the loop. It exists for repair: when the
Nymeria agent itself is broken (every turn errors, the provider is down, a
self-modification went wrong), the owner can still message Claude Code from
Telegram, the desktop, the CLI, or the MCP `nymeria_command` surface and have it
fix Nymeria on the host.

```text
/code [--new] [--resume] [--mode <mode>] [--dir <path>] [prompt]
```

- **Independent of the agent.** Slash commands are routed through
  `POST /commands/execute` before the chat path on every client, so `/code`
  works while chat turns are failing. The handler
  (`core/command_executor_claude_code.py`) touches no graph and no LLM.
- **Same transport and sessions as the tool.** It reuses
  `tools.claude_code.prepare_run`, so the runner remains the policy boundary
  (bearer token, cwd allowlist, deny rules, budgets), and the
  `(thread, project) -> session` map is shared: a `/code` follow-up continues
  the session the tool started on that thread, and vice versa.
- **Mode defaults to `bypass`** (`bypassPermissions`). This is the owner's
  explicit decision: the point is unattended repair, and the host-side deny
  list (`NYMERIA_CLAUDE_CODE_DISALLOWED_TOOLS`) still applies in every mode.
  Precedence mirrors the tool: an explicit `--mode`, then the thread's
  `claude_code_mode` override, then this default (the global
  `NYMERIA_CLAUDE_CODE_DEFAULT_MODE` is the tool's default, not this
  command's). `--mode plan` makes Claude Code write a plan and stop.
- **Session continuity per thread.** By default each `/code` resumes the
  thread's last Claude Code session, so when Claude Code stops to ask a
  question or present a plan, `/code <reply>` answers it in the same session.
  `--new` starts fresh; `--resume` is accepted for clarity and changes nothing.
  Bare `/code` shows the thread's state: the run in flight, the last outcome,
  and the session the next prompt would resume.
- **Reply, then follow-up.** The command waits up to 20 seconds for the
  run's FIRST report. A quick run answers inline (the command reply IS Claude
  Code's message plus the run summary; an INTERIM first report is returned
  as an info reply and the rest follow). A longer run gets an immediate
  acknowledgement carrying the job id, and every report (each interim
  end-turn, then the final) arrives in the same chat as its own model-free
  holder turn, tagged with the job and session. One run per thread at a
  time; a second `/code` is refused until it ends (the tool's
  `resume="<session>"` queues a follow-up on it instead).
- **Delivery needs no model.** A detached result is handed to the thread by
  `core/claude_code_delivery.py` as a short model-free holder turn
  (`completion_delivery.deliver_without_turn`): the thread is held the way a
  real turn holds it (lock, holder metadata, the pending queue's release
  window), the exchange is written into thread history under that hold (a
  hidden wake-up carrying the output as data, then the relayed text as the
  assistant message, after patching any dangling tool calls a dead turn left
  behind), a turn stream buffer is opened and fed the text, and the
  `task_started` / `task_completed` bookends go out. The buffer opens BEFORE
  `task_started` because the bots attach on that event to the thread's
  current buffer: publishing first would hand them the previous turn's
  retained buffer and swallow the result. Bots and the desktop therefore
  render it exactly as any autonomous turn; the activity ledger records it.
  If a live turn keeps the thread past 10 minutes (or the turn publish
  fails), the result falls back to a notification: bots post it as a plain
  message and the desktop gets an in-app item, never silence. A quick inline
  reply is recorded into history the same way (briefly, skipped if a turn
  holds the thread) but publishes no bookends, since the reply already
  reached the chat.
- **Cancellation and timeouts.** A `/code` run holds no thread lock, so both
  `/stop` surfaces (the command and `POST /threads/{id}/stop`) consult the
  run registry and set the run's own cancel event (the same signal the tool
  gets from the thread abort); a cancelled run stays silent. The runner's
  hard ceiling (one hour) still bounds a runaway run; the result is then an
  error reply. Telegram's per-thread autonomous delivery mode gates the
  follow-up like any autonomous completion (`notify_only` delivers only
  errors), so keep the default on a thread you would use for repair.

```text
/code the agent errors on every turn; read `docker logs nymeria-api` and fix it
/code --new --mode plan tighten the /status output
/code yes, go ahead with option 2        # answers the plan in the same session
```

## Usage examples

```text
# Add an idea to the backlog with context (the first use case)
claude_code("Add a backlog item under docs/private/plans/backlog for <idea>, "
            "with the usual Interpretation/Surface/Implications fields.",
            mode="bypass")

# Plan first, confirm, then execute
claude_code("Refactor the X module to do Y", mode="plan")     # returns a plan
claude_code("Looks good, implement it", mode="accept_edits")  # resume + execute

# Fire and continue talking; every end-turn arrives as a tagged follow-up
claude_code("Run the full test suite and fix any failures", detach=True)

# See what a running job is doing without resuming it
claude_code(peek="a1b2c3d4", tail=20)

# Follow up on a SPECIFIC session (queued if it is still running)
claude_code("Also add a changelog entry", resume="2e60c2a6-4c3d-4d7f-9192-1c63956949c5")
```
