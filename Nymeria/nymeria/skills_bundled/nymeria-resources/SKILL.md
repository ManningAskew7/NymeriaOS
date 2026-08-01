---
name: nymeria-resources
description: Map of Nymeria's on-disk resource stores and the rules for editing them
  directly as files. Load this when you want to inspect or change hooks, triggers,
  skills, custom tools, workflow definitions, MCP server configs, thread configs, or
  prompt overrides through the filesystem (file_read, file_write, file_edit,
  bash_execute) instead of the purpose-built tools, or to find where any Nymeria
  resource lives on disk, or to find out why a file tool refused a path under the
  resource root. This skill carries guidance only; it binds no tools.
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
   Use auth_write and auth_test (credential-management skill).
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
