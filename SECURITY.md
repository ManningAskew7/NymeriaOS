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
applied per exec) is partly shipped. `bash_execute` now runs its commands inside
it on Linux, which closes the environment-disclosure route described in Section
2.5, and on a deployment whose data directory sits outside the agent's working
tree it also closes off-disk reads of the credential stores. The two surfaces
that run agent-AUTHORED code out of process, the Python custom-tool runner and
the workflow runner, are inside it too, as is the `run_command` hook action. It
is a subtraction from what a command
could reach before, not an allowlist, and it is **not** tenant isolation: the
sandboxed command still reaches every other account's profile and transcript
data, and the remaining execution surfaces (MCP stdio servers, the MCP package
installer, the Claude Code bridge, and the
self-modification import check) are not wired to it yet. Until they are, run one trust domain per backend. Each unwired surface has to say at its own call site
why it is not confined; `tests/test_exec_sandbox_gate.py` fails the build on a
new agent-reachable spawn that does neither.

The confined surfaces do not all get the same policy, and the difference is
worth knowing. What can be denied depends on where a surface's child is
expected to create files, because Landlock cannot deny a path without making
its parent opaque to files created later. `run_command` runs hook scripts with
the data directory as their working directory, so on every layout its child
keeps read access to the credential stores and loses only `/proc`;
`bash_execute` works in the project tree, so on a deployment whose data
directory sits outside that tree it loses the stores as well.

Three things the sandbox does not do, worth knowing before relying on it:

- It confines reads, not writes. The policy grants write access across the
  filesystem and leaves ordinary file permissions to restrain it, so in a
  source checkout owned by the account Nymeria runs as, a command can rewrite
  Nymeria's own code, including the sandbox module. What that no longer buys is
  the NEXT call: the sandbox module is read once at import and shipped to the
  child as source, so a rewrite waits for a restart like any other module (see
  the detail below). The Docker shape mounts the package read-only, which is
  why it is the stronger deployment here as well. Self-modification is a
  shipped feature, so this is a consequence of the trust model rather than a
  bug in the sandbox.
- It does not confine the process tree's privileges beyond what Landlock
  requires. Requiring `no_new_privs` does disable setuid binaries and file
  capabilities as a side effect (`sudo`, `ping`, `mount` stop working), but
  that is a consequence, not a designed control.
- A kernel that permits unprivileged user namespaces would let a command mount
  a fresh `procfs` under a granted directory and read process state through it,
  because Landlock resolves rights up the mount chain. Ubuntu's AppArmor
  restriction and Docker's default seccomp profile both block this on the
  reference deployment; a host without either should not be treated as
  covered.

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

  One sharp edge to know: MCP resolves credential aliases **as the platform**,
  not as a user, so the vault's owner check is skipped there by design and
  `allowed_targets` is the only gate on that path. That list is now load-bearing
  rather than advisory: an empty list DENIES, and a credential saved without a
  stated purpose gets the set of call sites its kind is actually read from,
  which for every kind except an MCP credential excludes `mcp_server`. So an
  ordinary API key cannot be reached through the one reader that skips the owner
  check: a server definition that simply names such a credential fails closed at
  spawn. Reaching it takes an explicit bind, and binding is admin-only.

  Two residuals to keep in view. A row explicitly set to `["*"]` is readable by
  any target including MCP, and that is what a credential of an unrecognised
  kind gets, because narrowing a kind whose readers were never enumerated fails
  silently (the caller swallows the denial) while leaving it wide fails visibly;
  the kind is free text at the API and in the GUI, so an operator can create one
  by typing an unfamiliar word. And binding is what widens a row, which is
  correct for an admin wiring up a server but means the check that matters is
  the one on the bind: `POST /credentials/{id}/bindings` is owner-checked, while
  `install_mcp_server` asserts admin rather than checking ownership, since every
  route reaching it is admin-only today. Delegating MCP management to non-admins,
  a prerequisite for the multi-tenant work in 2.4, requires fixing that first.
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
  - **A command the agent runs can no longer read `/proc` file content**
    (Linux; `EXEC_SANDBOX_ENABLED`, on by default). The Landlock sandbox of
    Section 2.4 denies every file under the hierarchy apart from a few
    machine-wide ones (`meminfo`, `cpuinfo`, `stat`, `loadavg`, `uptime`,
    `version`), so the process-table tools do not work in a sandboxed shell and
    neither does reading another process's environment. Content, precisely:
    `/proc` stays LISTABLE, so a command can still enumerate PIDs and see the
    symlink targets in `/proc/self/fd`. The file tools refuse the same paths
    (they run in the API process, where Landlock cannot reach them), so the two
    channels agree. What remains open is any surface not yet sandboxed, listed
    in 2.4, and the API process's own in-process code.
  - **In the Docker shape this channel is open to anything not sandboxed, and
    you should assume it is open.** The compose services run with `init: true`, so PID 1 is
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

  Off-disk reads of the encrypted stores are closed for the sandboxed surfaces
  where the data directory sits outside the agent's working tree, which is the
  layout the Docker deployment already uses (project root `/app`, data dir
  `/data`).
  Where it sits inside the working tree, as in a source checkout, they stay
  open for `bash_execute` and the Python custom-tool runner: Landlock cannot
  deny a path without making its parent directories read-opaque for files
  created afterwards, which would stop ordinary commands reading what they had
  just written, so the denial is dropped rather than paid for. Move the data
  directory out of the working tree to get it. The workflow runner is the
  exception and keeps the denials in every layout, because its child works
  only in an ephemeral run directory and so needs no concession for the
  project tree.

  **The sandbox defends its own launcher, but only within a process
  lifetime.** The shim that applies the policy used to be launched by file
  path, which made it the one piece of the backend re-read from disk on every
  launch; since it runs before the policy exists, anything able to write that
  file lifted the sandbox for the next launch with no restart. It is now read
  once at import and handed to the interpreter as source, so a rewrite on disk
  cannot take effect until the process restarts. That is the same property the
  rest of the backend already had, not a stronger one: a rewritten module does
  nothing until it is reloaded, and after a restart the tampered code is what
  loads. Landlock cannot close the remaining half, because the project tree has
  to stay writable for self-modification to work, and denying a file would not
  stop it being replaced. So an actor that can write the source and wait for a
  restart still wins, which is `2.2`'s standing concession rather than
  something specific to the sandbox. Exposure is a deployment property: the
  reference Docker deployment does not grant it, because the container user
  does not own the bind-mounted source (verified: the write is refused), while
  a source or slim install running as the user who owns the checkout does. Run
  those as a separate user from the one owning the tree if that matters to you.

  **The interpreter underneath it is a live no-restart residual on some
  layouts.** Only Nymeria's own file stopped being re-read per launch. The
  launch still reads the Python binary, and Python still imports `os`, `json`,
  `struct`, `ctypes` and friends as ordinary stdlib files, all before the
  policy is applied, so where that tree is writable by the service user the
  original two-call shape survives against the stdlib instead. Measured: a
  `uv`-managed interpreter is mode 664 owned by the service user, while a
  distro `/usr/lib/python3.x` and the Docker image's interpreter are
  root-owned. So the Docker and system-Python shapes are unaffected and a slim
  install on a `uv` interpreter is not. This is not fixable inside the sandbox,
  because the shim has to be Python to reach Landlock at all and every import
  it makes is by construction pre-policy. Reaching it still requires code
  execution in the first place, which means `bash_execute`, an admin-only
  Python tool, or an admin-approved workflow. Run the service as a user that
  does not own its interpreter if this matters to you.

  The same rule leaves the deployment's `.env` readable to a sandboxed shell in
  EVERY shape, including Docker (the repo is bind-mounted at `/app`, so
  `/app/.env` comes with it). Dotenv files live in the project root by
  construction, and the project root is always a working tree, so this is not a
  denial that was forgotten: it is one the mechanism cannot express. The
  sandbox closes the process environment and leaves the file the environment
  was loaded from, and the file tools read it in every shape too. **The vault
  protects secrets from the model, and now from a sandboxed shell reading
  `/proc` or the data directory, but not from one reading the project root.**

  The gate is syntactic, so treat a green build as "no new bare spawn was
  introduced" rather than "nothing inherits". It cannot see a spawn dispatched
  through an indirection, nor a library that shells out internally (pydub
  invoking ffmpeg, Playwright launching its node driver, `webbrowser.open`).

Secrets at rest: the vault's secret columns and encrypted snapshots are
AES-encrypted, but the master key itself is a plaintext environment variable
(the root of trust), and an encrypted snapshot escrows that key under a single
passphrase. Protect the key material, the `.env` file, the data directory, and
snapshot passphrases accordingly.

**Where those credentials are allowed to go.**

A user-writable field must not decide where the server sends the server's own
credentials. That one rule has three shapes in this codebase, chosen by how
much freedom the destination legitimately needs:

- **(a) Replaced from the registry**, when the acceptable address is fixed and
  already declared. OAuth token endpoints work this way.
- **(b) Compared against configured destinations**, when the set is enumerable
  and letting a caller name one is a feature. LLM base URLs work this way.
- **(c) Joined to the credential's own record**, when the host is inherently
  arbitrary. Service integrations work this way.

*OAuth token endpoints.* The endpoint a refresh or code exchange POSTs to
receives whatever proves the grant: the operator's OAuth `client_secret` where
the provider is a confidential client, and the user's own refresh token where
it is a public one. Every provider Nymeria supports has exactly one such
endpoint, declared in `config/oauth_providers.py`. It is therefore taken from
that registry and never from the credential record, whose metadata any caller
who can reach `POST`/`PATCH /credentials` can write. The recorded `token_uri`
is kept for display and is not read back. This is a replacement rather than a
validation on purpose: validating leaves "which addresses are acceptable" open
indefinitely, while replacing closes it.

*LLM base URLs.* Two settings surfaces let a caller name an endpoint: the
admin-only global config, and the deliberately non-admin per-thread
`llm_config.base_url`. For the second, the destination may come from the
request but the stored credential may not.

A per-thread address the deployment is already configured for (it matches
`LLM_BASE_URL`, `LLM_BACKGROUND_BASE_URL`, or the provider's own configured
base URL) is not a redirection and behaves exactly as before, which is what
keeps per-thread CLIProxy routing working. A proxy that fronts several
providers on one host counts as configuration naming a destination for each of
them, so routing a thread to a different provider on the operator's own proxy
is not a redirection either. The three interchangeable spellings of the local
machine (`localhost`, `127.0.0.1`, `::1`) are compared as one host, since they
are one host; the rest of `127.0.0.0/8` deliberately is not, because a spare
loopback address is something a local process can bind and a canonical one is
not.

The provider's canonical vendor host counts only when configuration names no
destination for that provider, because on a proxied deployment the provider key
is a proxy-local secret and sending it to the vendor would be the disclosure
this control exists to prevent. "For that provider" rather than "at all": one
global base URL must not suppress the canonical host of a different provider
whose key really is that vendor's.

Any other address is refused unless the credential that would ride to it is
the caller's own rather than the deployment's, in which case there is nothing
to protect. Whose it is follows the key precedence exactly (per-thread
override, then a vault record, then the environment), and is judged by
provenance rather than presence: a literal counts, a `${credential:...}`
reference counts only when it names a record the caller owns, and an
environment key never counts. That distinction is load-bearing, because the
vault deliberately lets any identified principal read the deployment-wide
record, so a caller can point at the operator's key without holding it. The
same test is applied to every source a destination can come from, including a
vault record the caller wrote, not only to the obvious per-thread field.

A refused address is ignored rather than fatal: the turn runs on the
configured provider and the thread is told once. The check sits where the
value is consumed, not on the route, because the per-thread config file is
reachable by `file_write`. There is no role exemption, at either consumption
point, for the same reason: a planted config file's author is not the thread's
owner, and on a single-user deployment the only account is an admin.

*Service integration destinations.* A third-party integration takes both its
address and its credential from the vault, as separate lookups that used to
resolve independently: one record could supply the base URL, host fragment or
OAuth token endpoint while a different record supplied the secret that rode to
it. Any identified caller can create a record, and any caller's lookup can see
the deployment-wide one, so that split was reachable over the REST API.

The address is now served only by the first record holding any of the
provider's own credential fields, and only when that record holds every such
field any other visible record holds. Both halves matter: a provider with two
independent secrets (a delivery token and a preview token, an API key and a
session token) would otherwise let a record prove possession with one and steer
a request authenticated by the other, and a credential explicitly bound to a
tool outranks an unbound one, so position has to be checked as well as content.
Which fields count as an address and which count as possession is a declared
register rather than a name heuristic, and a build gate fails when a field the
integrations ask for is in neither half.

A refusal stops the call rather than redirecting it. That is deliberate and it
is the opposite of how the sibling LLM check behaves, for a reason specific to
this surface: an integration resolves its address as "the vault, or the
deployment's setting, or a hard-coded vendor host", and the last of those is a
third party. Treating a refusal as "nothing saved" would therefore not prevent
the request, it would send a self-hosted instance's credential to the vendor's
public API, which is the disclosure this rule exists to prevent. So a record
that holds an address it may not serve raises, naming the field and the two
ways to resolve it, and only a genuine absence falls through to the default.

The cost is that a deployment which deliberately splits one provider's address
and credential across two vault records, or keeps the address in the vault and
the credential in the environment, has to put them together or move the address
into settings. That split is precisely the shape being closed, and it cannot be
told apart from the planted version from inside the vault. Because the refusal
raises rather than returning nothing, it also pre-empts the deployment's own
`*_BASE_URL` setting on that call: an operator with both a stray record and a
configured address gets the error rather than the configured address. The
message names the field and both remedies.

A record's provider name is part of the check, not cosmetic. Two integrations
resolve their address under a provider's full alias list but each of their two
credentials under one alias, so a record saved under the other alias is visible
to the address lookup and invisible to the credential lookup. It could satisfy
the join by naming a credential field it would never be asked for. Where the
visible records disagree on the provider name, the join cannot tell which of
them a later lookup will reach, so it declines.

What this does not do is verify that a record's fields hold what they claim.
Field names are caller-chosen, so a record can satisfy the join by naming every
credential field with junk values. It then also wins those secret lookups, so
what reaches the address it chose is its own junk rather than anyone else's
secret. The exact guarantee (this address came from the same record as this
credential, checked where the request is built) is tracked separately.

*Where the tool request leaves.* The join decides which record may supply an
address. A separate control decides whether that address may be reached at all,
and it has to run at a different moment. Parsing a base URL happens once,
cheaply, and can only judge the text: it rejects a literal private, loopback or
metadata address, and a hostname tells it nothing. So every tool request is now
re-evaluated against the egress policy at the point it leaves, with DNS
resolution on, and the approved addresses are pinned for the duration of the
send. The pin is not decoration: without it the check and the connect perform
independent lookups, and a name that answers publicly for one and privately for
the other passes both. The two calls that reach a third-party SDK rather than
the shared HTTP client, both S3-shaped endpoint overrides, are screened where
they are resolved instead, since no later control can see them.

A proxy defeats all of that, so tool requests no longer use one. An
`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` setting sends the socket to the proxy and
lets the PROXY resolve the name, which makes both the private-address block and
the pin advisory, silently and only on the deployments that set those variables.
Both HTTP stacks in the tool package now decline that routing while keeping the
rest of their environment handling, so a custom CA bundle still applies and only
proxy routing is gone. A deployment that genuinely needs a forward proxy is
asking for an egress path this policy cannot see through, which is a decision to
take deliberately rather than inherit from an environment variable. `bash_execute`
is separate and unchanged: it runs under its own scrubbed environment and sees a
proxy variable only if the operator names it in `BASH_ENV_PASSTHROUGH`.

The address the policy judges is also normalized to the one a client will
actually dial. An internationalized hostname has two encodings, and the stdlib
and the libraries these tools use implement different standards, so for the
characters where the two disagree a policy that read one spelling would have
been describing a different domain than the socket reached.

This is a capability change for one deployment shape, and there is no way to
have the control without it. A self-hosted integration reached by hostname
(`http://jenkins.corp.local:8080`) was previously allowed and is now refused,
where the same instance reached by IP was already refused. Both are answered the
same way, by naming the host in `HTTP_INTERNAL_ALLOWLIST`, which is the existing
mechanism for exactly this.

An endpoint override the operator configured in settings rather than the vault
is left alone, because `S3_ENDPOINT_URL=http://minio:9000` is an ordinary
deployment and settings are a much narrower surface than the vault: no native
tool writes them. Narrower is not sealed. `s3_endpoint_url` is in the
`PATCH /settings` schema and `nymeria_update_settings` exposes that route over
MCP, so on a deployment where an ADMIN's thread mounts Nymeria's own MCP server
the value is reachable from a turn. That is the same line drawn everywhere else
in this document: an admin's own agent holds what the admin holds, and a control
that tried to stop it would be pretending otherwise. The exemption is also only
available at the two sites where the provenance of the value is known for free;
the shared HTTP path cannot tell which leg produced an address and screens it
either way.

Known residuals. A caller supplying their own key may still name any address,
including a loopback one, so this is not an SSRF control. The gate keys on the
address, so a per-thread `provider` switch with no address of its own is not a
redirection and is not gated; on a deployment that sets the generic `LLM_API_KEY`
rather than per-provider keys, that sends the generic key to the newly chosen
provider's own canonical host. A thread pointed at a
keyless local model server has to set some per-thread `api_key` as well, since
otherwise the refusal would send its prompts to the configured provider
instead. On the service-integration side the join covers addresses only: a
record holding a TLS-verification flag, a header set, or an API-key header name
can still weaken a request another record authenticates, since those are not
addresses and the join does not look at them. The join also compares vault
records to each other, so it cannot see a credential that comes from the
environment. That is the larger of the two gaps: a record needs only some
credential field of the provider's, not the one a given call site will ask for,
so it can satisfy the join with a field nothing asks for and let both real
lookups fall through to the operator's environment. The two S3-shaped endpoint
overrides check this directly, because there the address and the key pair are
resolved in one place; the general form waits on resolving a provider's whole
credential set from one record rather than field by field. Embedding and voice
base URLs are deliberately not screened: they are admin-only global settings
with no per-thread override, and pointing them at localhost is the documented
way to run a local embedder or a local voice server, so a private-address block
there would remove a supported configuration without removing an attacker. If
either ever gains a per-thread override it needs a screen, and voice needs the
async egress path first, since the DNS pin does not survive an
`httpx.AsyncClient`.

### 2.6 In-process heuristics (useful, not boundaries)

These components screen or gate behavior. They are worth having. None is a
security boundary, and several say so in their own code.

- **`command_guard` shell blocklist.** A small hardline list (`rm -rf /`, `mkfs`,
  fork bombs, self-kill of the process/container). It catches a hallucinated or
  naively injected destructive command at near-zero false-positive cost. It is a
  blocklist over a Turing-complete shell; a determined caller bypasses it
  trivially.
- **Content-hash execution gates** (MCP servers, Python custom tools, workflows,
  and HTTP/MCP custom tools).
  Each recomputes a hash of a definition's live on-disk content at execution and
  fails closed on mismatch, so a definition planted on disk or hot-loaded outside
  the sanctioned admin path stays inert. These are provenance checks, not
  sandboxes; a caller who already has file-write or shell bypasses them.
- **File-tool path policies.** Two of them. A denylist blocks
  `file_read`/`file_write`/`file_edit` from the credential and token stores. A
  role check refuses the two write tools (not `file_read`) on the global prompt
  overrides in the data-dir root for anyone who is not an admin, mirroring the
  admin gate their REST surface already carries. Neither governs
  `self_modify_rollback`, which has its own screen: it restores only into the
  self-modification writable allowlist, which is disjoint from the data dir, so
  the backup stash cannot launder a write into either set. Tool-layer foot-gun
  guards for the structured file tools only; `bash_execute` reads and writes
  those files directly.
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

- **Hooks fire once per call, under the tool's own name, on every dispatch
  path.** `pre_tool_use` and `post_tool_use` fire in one shared execution
  envelope (`core/tool_execution.py`) that the graph's tool node and the by-name
  paths (`tool_invoke`, the workflow SDK's tool verbs, `self_invoke_tool`) all
  pass through, so a hook authored against a tool constrains that tool however
  it was reached. `tool_invoke` and `self_invoke_tool` are treated as transport:
  the hook sees the tool they dispatched, not the meta-tool, which also means a
  hook authored against either literal transport name does not fire. A call
  refused by the gate before
  the envelope (role, denylist, `disabled_tools`) fires no hook at all, because
  it never ran.
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
