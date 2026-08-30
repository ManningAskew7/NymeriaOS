---
name: nymeria-resources
description: Map of Nymeria's on-disk resource stores and the rules for editing them
  directly as files. Load this when you want to inspect or change hooks, triggers,
  skills, custom tools, workflow definitions, MCP server configs, thread configs, or
  prompt overrides through the filesystem (file_read, file_write, file_edit,
  bash_execute) instead of the purpose-built tools, or to find where any Nymeria
  resource lives on disk, or to find out why a file tool refused a path under the
  resource root. Also covers the deployment's env/settings files, which follow
  different rules, and why a hand-edited setting appears to take effect without
  actually applying. This skill carries guidance only; it binds no tools.
---

# Nymeria Resource Filesystem

Every agent-owned Nymeria resource is a plain file under one directory, the
**resource root**. The generic file tools are an equally supported management
surface for those files; the purpose-built tools (tool_create, hook_config,
trigger_config, skill_write, mcp_manage, workflow_info) remain the validated
fast path. This skill is the durable map; the live per-deployment index is
`README.md` at the resource root.

## Finding the root

- The `bash_execute` and `file_*` tool descriptions carry a
  "Nymeria resource root" line with the resolved absolute path. Trust that
  line over any remembered path.
- Bare metal and slim: `<project_root>/data`, so relative `data/...` paths
  work from the default cwd.
- Docker: `/data` (a volume), with an `/app/data` symlink so the same
  relative `data/...` paths work. The code tree (`/app/nymeria`) is
  read-only there; resources are not.
- Read `README.md` at the root first: it is generated from the running code
  at every startup and lists each store's path, scope, hot-load behavior,
  and edit posture. `schema/*.schema.json` beside it describe the JSON file
  formats.

## The stores (generic layout)

| Store | Path under the root |
| --- | --- |
| Custom tools + workflow definitions | `custom_tools/<id>.json`, source at `custom_tools/revisions/<tool_id>/<hash>.py` |
| Lifecycle hooks | `hooks/<user>.json` |
| Triggers | `triggers/<user>.json` |
| Skills and kits | `skills/global/<name>/SKILL.md`, `skills/users/<id>/<name>/SKILL.md` |
| MCP servers | `mcp_servers/<id>.json` |
| Thread configs | `thread_configs/<thread_id>.json` |
| Prompt overrides | `system_prompt.md`, `dream_prompt.md`, `dream_kickoff.md` (absent = built-in default; readable by anyone, writable only on an admin's turn) |

Kits are ordinary skills whose frontmatter declares
`metadata.nymeria.required_tools`. Bundled skills ship inside the package,
are read-only, and are shadowed by a global or user skill of the same name:
to change a bundled skill's behavior, write a same-name skill under
`skills/global/` instead of hunting for the bundled file.

## Editing rules

1. **Check hot-load before relying on a raw edit.** The README's hot-load
   column is authoritative for the deployment you are on. Where it says
   "yes", an edit is picked up on next use. Anything else means the store
   is read at startup or generated: after a raw edit, apply it the way the
   column says (settings update, restart), or your change silently does
   nothing.
2. **Approval gates stay in force.** Python custom tools and workflow
   definitions are content-hash gated: a raw source edit is allowed but the
   tool refuses to run ("approval_required") until an admin re-approves via
   tool_create publish or the workflow approval surfaces. This is by
   design; do not try to work around it.
3. **Credentials are off limits.** The account vault and OAuth token caches
   are not part of this interface and the file tools refuse them for everyone.
   Use auth_write and auth_test (credential-management skill). `/proc` is
   refused for the same reason: it exposes the running deployment's whole
   environment, secrets included. When a resource file legitimately NEEDS a
   secret (an MCP server's env block, a custom tool's auth header), reference
   it instead of inlining it: `${credential:ID.FIELD}` resolves from the vault
   and `${env:VAR}` from the process environment, both at use time, so the
   file on disk never holds the secret.
4. **The global prompt overrides are admin-only to write.** Each of
   `system_prompt.md`, `dream_prompt.md` and `dream_kickoff.md` replaces a
   prompt for every user of the deployment, so the file tools apply the same
   rule their settings routes do: reads always work, writes and edits succeed
   on an admin's turn and are refused otherwise. If you are refused, do not
   route around it with bash_execute; say what you wanted to change and ask.
   To steer one thread instead, use its per-thread instructions or dreaming
   config, neither of which needs an admin.
5. **Do not fight the tools.** Never raw-edit a store that the current turn
   is also mutating through its tools; store writes are whole-file
   last-writer-wins.
6. **Clients will not see raw edits immediately.** Raw edits emit no UI
   events; desktop/mobile panels refresh on next fetch.
7. **Mind the blast radius on per-user files.** `hooks/<user>.json` and
   `triggers/<user>.json` hold that user's entire store in one file; a
   malformed write degrades the whole store. Validate JSON against
   `schema/` before writing, and prefer file_edit (targeted replace) over
   whole-file rewrites.
8. **Operational state is not yours to edit.** Execution logs, approvals,
   run traces, backups, logs, databases, and the `<store>.json.sig`
   fingerprint sidecars beside the hook and trigger stores: readable for
   debugging, but edits can corrupt runtime state. The README lists them.
9. **Verify by the mechanism, not by a read-back.** Reading a value back
   proves the file or the cache holds it, never that the running system uses
   it. Confirm the way that store actually applies changes (invoke the thing,
   check the hot-load column, look for the event), and when you cannot
   confirm, say the change is written but unapplied rather than reporting
   success.

## Deployment config is a different thing

The env files (`.env`, `config.env`, `.env.docker`) live at the PROJECT root,
not the resource root, and are not in the table above. They hold the
deployment's settings and secrets, they are the user's operational state, and
they follow none of the rules above.

**Change a setting with `/env set` or `/settings set`.** That path writes the
file, exports just that key into the running process, clears the settings
cache, rebuilds the agent's graph when an LLM, tool or credential field
changed, tells you whether a restart is still required, and alerts the owner
on the egress-shaped keys. A few global switches are refused on an agent's
turn by design: ask rather than routing around them.

**A raw edit to an env file half-applies, and the halves are the opposite of
what you would guess:**

- A key the process already holds with a NON-EMPTY value is shadowed. The
  environment outranks the file, so the edit does nothing at all until a
  restart. After a normal start that is every key the file had at boot.
- A key that is absent from the environment, blank there, or cleared by the
  runtime IS picked up by the next settings reload, but only into the settings
  object. The compiled graph is not rebuilt for it and nothing reports that a
  restart is needed.
- So a read-back that agrees with your edit proves nothing: `/env get` can
  report a new model while every turn still runs the old one.

**Never append a second line for a key that already exists.** The writer
updates a key's first occurrence and dotenv readers take the last, so an
appended line leaves the two disagreeing and the change reverts at the next
restart. Edit the existing line.

**A key you just added to the file is not in the environment yet**, so
`${env:NEW_VAR}` in an MCP server or custom tool will not resolve until a
restart (the MCP path substitutes empty, a custom tool raises). Put it in the
vault and use `${credential:...}` if you need it this session.

**Applying an env-file edit properly means restarting the API.** That is a
deployment-level action you cannot take yourself and should not treat as
free: it drops in-flight turns and streams, and on some deployment shapes it
needs the user at a terminal. Say what you changed, say a restart is needed
to apply it, and let the user pick the moment.

## Weight of a raw edit

A written hook is a standing prompt injection into future turns; a written
trigger is scheduled autonomous action; a prompt-override edit silently
changes the persona of every thread in the deployment, which is why that one
is admin-only. Treat raw writes to those stores with the same
care as granting yourself new powers: do it because the user asked, say so
in your reply, and keep the change minimal.

## When to use which surface

- **Purpose-built tool**: creating something new, anything the tool
  validates (schemas, names, approval flows), anything the user will watch
  from a client UI.
- **Raw files**: bulk inspection (`bash_execute` + grep over the root),
  surgical edits to existing definitions, diffing, backup/restore of your
  own resources, and anything the tools do not expose yet.
