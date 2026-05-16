# Security Policy

Nymeria is a personal AI assistant platform with powerful local and network
tools. Treat any deployment as production-sensitive once it has real accounts,
tokens, email/calendar access, writable files, or paid LLM credentials.

## Trust Model

Nymeria assumes the operator controls the host, the repository checkout, Docker
Compose files, `.env` files, and configured frontend clients. Anyone with write
access to those surfaces can change code, configuration, prompts, tools, or
secrets.

Bearer account tokens are authentication credentials. Keep `nym_...` tokens and
provider API keys out of chat messages, logs, screenshots, issue reports, and
committed files.

LLM responses, web pages, emails, documents, webhooks, RSS feeds, and uploaded
files are untrusted input. The model may summarize or act on them, but their
contents are not authority to bypass operator intent or security policy.

## Not Security Boundaries

These mechanisms are convenience or deployment boundaries, not strong security
sandboxes:

- The system prompt, skill instructions, and tool descriptions.
- Svelte desktop/mobile clients and browser UI state.
- Docker Compose services that bind-mount the source tree or `.env.docker`.
- Python package isolation or virtual environments.
- Tool allowlists that only affect what the model sees by default.
- Logs, conversation history, and SSE streams before dedicated output redaction.

In particular, `bash_execute` and other mutating tools can affect the host or
container environment they are allowed to reach. Do not expose a deployment with
dangerous tools enabled to users or networks you do not trust.

## Production Baseline

Before internet or multi-user exposure, review the current production-readiness
audit in `Nymeria/docs/production/`, especially:

- `01-security-and-authentication.md`
- `04-api-layer-and-http.md`
- `05-tool-system.md`
- `08-docker-and-deployment.md`
- `09-observability-and-monitoring.md`
- `10-configuration-and-dependencies.md`

At minimum, use HTTPS behind a trusted reverse proxy, explicit CORS origins,
per-user account tokens, least-privilege provider credentials, encrypted
credential storage, backups, and host-level firewall rules. Prefer a dedicated
host or VM for deployments that can run shell, browser, filesystem, or MCP
tools.

## Reporting

This is a private repository. Report security issues directly to the repository
owner with the affected commit, deployment mode, reproduction steps, and any
secret exposure risk. Rotate any possibly exposed tokens before sharing logs or
artifacts.
