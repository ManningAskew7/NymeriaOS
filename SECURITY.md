# Nymeria Security Policy

Nymeria is a pre-beta, single-developer personal AI assistant platform with
powerful local and network tools: a shell, filesystem read/write, HTTP and MCP
tool execution, code authoring, and browser control. This document states the
one boundary the project treats as load-bearing, names the in-process
mechanisms that are deliberately **not** boundaries, and defines the scope for
vulnerability reports.

Read Sections 2 and 3 before deploying beyond a single trusted machine, and
before filing a report. Treat any deployment as production-sensitive the moment
it holds real accounts, tokens, email or calendar access, writable files, or
paid LLM credentials.

---

## 1. Reporting a vulnerability

This is currently a private repository maintained by a single developer. Report
security issues privately to the maintainer, not in public issues. A useful
report includes:

- A concise description and your severity assessment.
- The affected component by file path and line range (e.g. `path/to/file.py:120-145`).
- The deployment shape (full Docker stack, single/slim container, or bare slim)
  and commit SHA.
- A reproduction, and a statement of **which trust boundary in Section 2 is
  crossed**.
- Any secret-exposure risk. Rotate possibly exposed tokens and provider keys
  before sharing logs or artifacts.

Please read Sections 2 and 3 first. Reports that only demonstrate the limits of
an in-process heuristic this policy does not treat as a boundary (Section 2.6,
Section 3.2) are welcome as ordinary issues or pull requests, but are not
security vulnerabilities under this policy.

---

## 2. Trust model

### 2.1 Definitions

- **Operator.** Whoever runs the Nymeria backend. Nymeria assumes the operator
  controls the host, the repository checkout, the Docker Compose and `.env`
  files, the data directory, and the configured frontend clients. Anyone with
  write access to those surfaces can change code, configuration, prompts, tools,
  or secrets.
- **Agent process.** The Python process running `NymeriaAgent` (in Docker, the
  `api` container). It holds the master encryption key and all provider
  credentials in its environment, and it is the only agent runtime.
- **Execution surfaces.** Tools and actions that run model-influenced code as a
  subprocess: `bash_execute`, Python custom tools, the `nym` workflow runner,
  the `run_command` hook action, MCP stdio servers and MCP install, and the
  host-side `claude_code` bridge.
- **Input surface.** Any channel through which content enters the model's
  context: operator input, chat-platform messages, web fetches, emails,
  documents, file reads, MCP/tool results, webhook payloads.
- **Trust envelope.** What the operator has implicitly granted the agent by
  running it: on the default posture, whatever the agent-process user can reach
  on that host.

### 2.2 The one boundary: OS / container isolation

**The only security boundary against an adversarial or prompt-injected LLM is
the operating system (in Docker, the container).** Nothing inside the agent
process is containment: not the shell command blocklist, not the content-hash
execution gates, not the file-tool secrets denylist, not output redaction, not
any tool allowlist. Each is a heuristic operating on an attacker-influenceable
string, or an admission check that a shell-capable caller bypasses.

This follows from one structural fact: `bash_execute` is a default-on tool, and
every execution surface runs as the agent-process user in the same container or
host. An agent that can run a shell already has arbitrary code execution at that
privilege. So the blast radius of a compromised turn (through prompt injection,
a malicious tool, or a malicious MCP server) is "whatever the agent-process user
can do on that host," and the thing that bounds it is the OS or container, not
application code.

### 2.3 Deployment postures and assurance tiers

Nymeria runs in three shapes with materially different isolation. Match the
posture to the trust of the content the agent will ingest.

- **Docker full stack (`docker-compose.yml`) — hardened; recommended.** The
  agent-bearing `api` container and its siblings run non-root (uid 999) with
  `cap_drop: ALL`, `no-new-privileges: true`, the default Docker seccomp
  profile, PID and memory limits, and (for most services) a read-only rootfs.
  The API and MCP ports bind to loopback; Postgres and Redis are on an internal
  network only; services are network-segmented so a compromised bot cannot reach
  the database directly. **In this shape the container is the boundary.** Note:
  the `api` rootfs is writable and holds all secrets, so a compromise inside
  `api` is a compromise of the secrets and the data volume regardless of the
  other containers' hardening.
- **Single / slim container (`docker-compose.single.yml`) — soft.** The same
  agent runtime, but the container currently ships **without** `cap_drop`,
  `no-new-privileges`, read-only rootfs, or PID/memory limits. It is a real but
  weaker boundary. Add those restraints before exposing it to anything
  untrusted.
- **Bare slim (`run.py slim`) — no container boundary.** The agent runs as your
  own user with your full access to the host. Appropriate only for a single
  trusted machine.

Running bare slim, or an unhardened single container, while ingesting untrusted
web, email, or message content is outside the supported security posture.

### 2.4 One trust domain today; multi-tenant is under construction

Today Nymeria runs as a **single trust domain**. The install wizard offers only
the "Unleashed" profile: no tool requires approval by default, and the full tool
set (including `bash_execute`) is enabled. The tiered "Secure" and "Standard"
profiles, which would gate tools by risk level, are designed but **not yet
enforced**.

There is a real per-user account model (token-derived roles, ownership checks,
admin-only impersonation) that scopes data correctly at the API layer. But that
scoping does not survive the execution channel: a shell-capable agent acting for
any account can read the entire shared data volume and the master key off the
host. **Do not treat two accounts on one backend as isolated tenants.**

Cross-tenant execution isolation (an unprivileged Landlock filesystem sandbox
applied per exec) is in active development. Its primitive exists in the tree but
is **not yet wired to any execution surface, so it enforces nothing today**. Do
not rely on it. Until it ships and is enabled, run one trust domain per backend.

### 2.5 Credential handling: two channels

Nymeria's credential vault is a genuine boundary on one channel and explicitly
not on the other. State both when reasoning about it.

- **Language channel (protected).** Secrets are stored Fernet-encrypted. Tools
  reference them by alias, `${credential:id.field}`. The server resolves an alias
  to plaintext only inside the tool executor, after the model has already
  emitted the call, and the resolved value goes onto the wire (an HTTP request,
  a subprocess env), never into any model-visible string. The model, the stored
  conversation checkpoint, and the LLM provider's message payload never see
  vault plaintext, and there is no agent tool that reads a secret back
  (`auth_write` is write-only). This holds against a language-level adversary: a
  prompt-injected model cannot read a stored key out of its own context.
- **Execution channel (not protected today).** The master key lives in the agent
  process environment. Any subprocess the agent spawns runs as the same user and
  can, by default, recover the key and then decrypt the stores off disk. Three
  separate channels, in different states:
  - Environment *inheritance* is scrubbed on the agent-reachable spawns in the
    API process and on the setup-wizard and CLI spawns (those load the whole
    deployment `.env`, so they mattered as much). A repo gate fails the build
    if a new spawn lands without a deliberate environment or a marker comment
    justifying the inherit. Five spawns are deliberately exempt and none is
    agent-reachable: the API re-executing itself on a self-restart, the wizard
    launching the slim backend, two `docker compose` invocations (compose
    resolves `${...}` from the process environment, and the environment is the
    stack configuration it is installing), and a statusline script the user
    wrote themselves, running on their own machine under their own account.
    The gate is syntactic and its limits are listed in its own module
    docstring; read a green run as "no new bare spawn was introduced", not as
    "nothing inherits".
  - `/proc/<pid>/environ` and ptrace reads of *the agent process* are closed by
    `PR_SET_DUMPABLE(0)`, set at startup in `run.py`, which makes that process's
    `/proc` entries root-owned. Root and `CAP_SYS_PTRACE` are unaffected, and
    `NYMERIA_DISABLE_PROCESS_HARDENING=1` turns it off for debugging at the cost
    of reopening the channel.
  - **In the Docker shape this channel is only partly closed, and you should
    assume it is open.** The compose services run with `init: true`, so PID 1 is
    the container runtime's init shim (tini), not Python, and it holds a
    byte-identical copy of the container environment: every provider API key,
    the service token, and the vault master key. It runs as the same
    unprivileged user, and a process cannot make *another* process undumpable,
    so nothing inside the container can close it. Verified on a reference
    deployment: `/proc/1/environ` is world-of-that-uid readable and complete
    while `/proc/<python>/environ` is not. Closing this properly means keeping
    secrets out of the container environment entirely (a mounted secrets file
    read at startup) rather than hardening a process; until that ships, treat
    the Docker shape as conceding the whole environment to any in-container
    shell. The slim shape has no init shim and is not affected.

  Off-disk reads of the encrypted stores remain open in both shapes, which is
  what the Landlock work (Section 2.4) targets. **The vault protects secrets
  from the model, not from a shell.**

  The gate is syntactic, so treat a green build as "no new bare spawn was
  introduced" rather than "nothing inherits". It cannot see a spawn dispatched
  through an indirection, nor a library that shells out internally (pydub
  invoking ffmpeg, Playwright launching its node driver, `webbrowser.open`).

Secrets at rest: the vault's secret columns and encrypted snapshots are
AES-encrypted, but the master key itself is a plaintext environment variable
(the root of trust), and an encrypted snapshot escrows that key under a single
passphrase. Protect the key material, the `.env` file, the data directory, and
snapshot passphrases accordingly.

### 2.6 In-process heuristics (useful, not boundaries)

These components screen or gate behavior. They are worth having. None is a
security boundary, and several say so in their own code.

- **`command_guard` shell blocklist.** A small hardline list (`rm -rf /`, `mkfs`,
  fork bombs, self-kill of the process/container). It catches a hallucinated or
  naively injected destructive command at near-zero false-positive cost. It is a
  blocklist over a Turing-complete shell; a determined caller bypasses it
  trivially.
- **Content-hash execution gates** (MCP servers, Python custom tools, workflows).
  Each recomputes a hash of a definition's live on-disk content at execution and
  fails closed on mismatch, so a definition planted on disk or hot-loaded outside
  the sanctioned admin path stays inert. These are provenance checks, not
  sandboxes; a caller who already has file-write or shell bypasses them.
- **File-tool secrets denylist.** Blocks `file_read`/`file_write`/`file_edit`
  from the credential and token stores. A tool-layer foot-gun guard for the
  structured file tools only; `bash_execute` reads those files directly.
- **Output and log redaction.** Targeted substring redaction of known secret
  values on known egress paths (custom HTTP tool results, MCP source, credential
  tests, provider errors, Redis URLs). It is not a comprehensive data-loss
  layer; for example `bash_execute` stdout is not redacted.
- **MCP stdio launcher allowlist.** Restricts stdio launchers to a known set
  (`npx`, `uvx`, `node`, `python`, ...). It constrains how a server starts, not
  what an allowed launcher can then do with a hostile package.

Treat all of these as accident- and naive-injection prevention layered on top of
the OS boundary, never as the boundary.

### 2.7 Authorization surfaces

The network and trust-edge boundaries are enforced and fail closed:

- **REST API.** Per-user `nym_...` bearer tokens (stored only as hashes),
  verified server-side on every route, behind a per-IP auth-failure rate limiter
  and per-user request limits. An unresolvable, revoked, or expired token is
  rejected (401). Bearer-unauthenticated routes are limited to health checks,
  the static SPA fallback, and the webhook / OAuth-callback receivers that verify
  their own signature, JWT, or state parameter instead of a bearer token. The
  legacy shared API key is no longer accepted. Admin is a token-derived role tier
  resolved from the account database on each request.
- **Act-as (`X-Nymeria-Act-As`).** Admin-only; a non-admin attempt is rejected.
  The effective `user_id` is always token-derived, never taken from a client
  parameter.
- **MCP HTTP server.** Requires an inbound bearer token resolved against the
  backend; non-admins are pinned to their own identity and any `user_id` argument
  is ignored. The override `NYMERIA_MCP_ALLOW_UNAUTHENTICATED` disables this gate
  and must never be set on a reachable endpoint.
- **Chat-platform bots** (Discord, Telegram, Slack, WhatsApp, Teams).
  **Default-deny:** a platform user who is not linked to a Nymeria account is
  rejected. A link is created either self-service by the account holder (a
  short-lived link code redeemed in chat) or by an admin on another user's
  behalf; a stranger with no account cannot self-link. Inbound HTTP webhooks are
  verified per provider: the WhatsApp webhook checks Meta's HMAC-SHA256 signature
  and rejects stale/replayed payloads; the Teams webhook validates a Bot
  Framework RS256 JWT (issuer, audience, expiry). Discord, Slack, and Telegram
  use authenticated outbound connections (gateway, socket mode, long-poll), not
  signed inbound webhooks.

One authorization path is an **OS boundary, not an application boundary**, by
design: the `nymeria users issue-token`, `rotate-token`, and `add --role admin`
CLI commands mint tokens (including admin tokens) directly against
`data/accounts.db` with no application authentication, so an operator can recover
access when the API is down. Consequently, application RBAC ultimately rests on
**filesystem permissions on the data directory** (`accounts.db`,
`SLIM_SERVICE_TOKEN.txt`, `BOOTSTRAP_TOKEN.txt`): anyone who can read or write
the data directory, or run the `nymeria` CLI as that user, is effectively admin.
Protect the data directory like the secret it guards. (Account passwords are not
implemented; authentication is bearer-token only.)

### 2.8 Tool use and prompt injection

LLM output, web pages, emails, documents, retrieved files, webhook payloads, and
sibling messages in a shared channel are **untrusted input**. The model may
summarize or act on them, but their contents are never authority to override
operator intent or security policy. Getting the model to emit unusual output,
through injected content or otherwise, is an expected risk; it is *contained* by
the OS boundary (Section 2.2), not *prevented* in the process.

Nymeria offers an opt-in, user-authored human-approval gate (the
`require_approval` lifecycle hook: an in-band hold that denies on timeout, abort,
or failure) and pre-tool guardrails (`block_if_matches`, `rewrite_arg`) for
irreversible or outbound actions. They are **not on by default and are not a
platform-wide injection defense**: there is currently no mandatory approval on
any tool, and the system prompt does not rely on in-prompt provenance framing as
a control.

Two limits matter more than the opt-in status, because an authored hook can read
as stronger than it is:

- **Hooks fire on one dispatch path, not both.** `pre_tool_use` and
  `post_tool_use` fire in the graph's tool node, which covers tool calls the
  model emits normally. They do **not** fire on the by-name invocation paths
  (`tool_invoke`, the workflow SDK's tool verbs, `self_invoke_tool`), which
  reach the same tools through a different dispatcher. An approval or
  `block_if_matches` hook authored against a tool therefore does not constrain
  that tool when it is invoked by name. Treat an authored hook as covering
  ordinary model tool calls only, until this is unified.
- **The action layer is not uniformly fail-closed.** Hook *dispatch* fails
  closed on the pre-tool path, but two actions invert that at the action layer:
  a `run_command` exit that is nonzero but not exactly 2, and `run_workflow`'s
  author-selected `on_fault`, both default to allow-with-note. Only exit 2
  denies. A hook that errors in those shapes lets the call through.

If you expose the agent to untrusted input and care about a specific
irreversible/outbound action, author a hook for it, know which dispatch path it
covers, and still treat the OS boundary as the real containment.

---

## 3. Scope

### 3.1 In scope

- **Escape from a declared OS-level posture (Section 2.3):** attacker-controlled
  code reaching state that posture claimed to confine (e.g. breaking out of the
  hardened full-stack container).
- **Unauthenticated or bypassed access to a network surface that should require
  authentication** (REST API, MCP HTTP, bots): an auth bypass, a fail-open path
  that is not a documented operator override, or a signature/replay bypass on a
  webhook adapter.
- **Credential exfiltration** to outside the trust envelope via a mechanism that
  should have prevented it: an environment-scrub bug, an adapter that logs a
  secret, a redaction path that should have fired, a transport error that flushes
  a credential upstream.
- **Vault language-channel leak:** a `${credential:...}` value reaching
  model-visible state, the conversation checkpoint, or a log.
- **Act-as or ownership bypass:** one account reaching another account's data or
  actions through the API despite the role and ownership checks.
- **Behavior contradicting this policy or the documentation**, including a code
  path that breaks a stance the docs state (for example, a client rendering
  agent output in a way the docs say is inert).

### 3.2 Out of scope (not a vulnerability under this policy)

"Out of scope" means "not a security vulnerability under this policy," not "not
worth reporting." Hardening ideas and heuristic improvements are welcome as
issues or pull requests.

- **Bypasses of the in-process heuristics (Section 2.6):** `command_guard`
  blocklist bypass, redaction bypass, forging a content-hash gate from a
  shell/file-write-capable position, or reaching a denylisted file via
  `bash_execute`. These are not boundaries; defeating them is expected.
- **Prompt injection per se:** getting the model to emit unusual output without a
  chained escape from an OS posture.
- **Consequences of the chosen posture:** shell or file tools reaching host state
  on bare slim or an unhardened single container; the agent doing what the
  default "Unleashed" profile permits.
- **Token minting from filesystem access (Section 2.7):** the `nymeria users`
  CLI issuing tokens presupposes host/data-directory access, which is already
  inside the trust envelope. Its offline-admin behavior is by design.
- **Documented fail-open overrides deliberately set:**
  `NYMERIA_MCP_ALLOW_UNAUTHENTICATED`, disabled approval hooks, bare slim in
  production, and similar operator trade-offs.
- **Cross-account isolation via the execution channel:** a shell-capable agent
  for account A reading account B's data on a shared backend is a **known
  limitation** (Section 2.4), not a new finding, until the execution-isolation
  work ships and is enabled.
- **Third-party MCP servers, custom tools, skills, and plugins** behaving
  maliciously after an operator installs them. Operator review before install is
  the control. (A bug in the install path that hides what is being installed is
  in scope.)
- **Public exposure without external controls:** exposing the API, MCP HTTP, or a
  bot to the internet without a reverse proxy, TLS, firewall, or VPN.

---

## 4. Deployment hardening

The single most important decision is matching isolation (Section 2.3) to the
trust of the content the agent ingests. Beyond that:

- **Prefer the full Docker stack**, or add `cap_drop: ALL`,
  `security_opt: no-new-privileges`, a read-only rootfs, and PID/memory limits to
  the single container before exposing it. Prefer a dedicated host or VM for any
  deployment that can run shell, browser, filesystem, or MCP tools.
- **Protect the data directory and key material.** Keep `NYMERIA_SECRETS_KEY`,
  the `.env` file, `data/accounts.db`, and the token files
  (`SLIM_SERVICE_TOKEN.txt`, `BOOTSTRAP_TOKEN.txt`) tightly permissioned and out
  of version control, logs, and backups-in-the-clear. Anyone who can read them is
  effectively admin (Section 2.7).
- **Terminate TLS at a trusted reverse proxy**, set explicit CORS origins (never
  `*` with credentials), keep the API and MCP ports on loopback, and add
  host-level firewall rules. Do not publish the MCP HTTP port; never set
  `NYMERIA_MCP_ALLOW_UNAUTHENTICATED` on anything reachable.
- **Use per-user account tokens** rather than sharing one, and least-privilege
  provider credentials.
- **Review third-party MCP servers, custom tools, and skills before install** —
  they run with the agent's privileges.
- **Back up and guard snapshot passphrases;** an encrypted snapshot embeds the
  master key and is only as strong as its passphrase.
- **Run one trust domain per backend** until cross-tenant execution isolation
  ships and is enabled (Section 2.4).

---

## 5. Disclosure

Nymeria is maintained by a single developer on a best-effort basis. Coordinated
disclosure is appreciated: allow a reasonable window for a fix before public
discussion. Reporters are credited unless anonymity is requested.
