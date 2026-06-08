# Agent Skills

Agent Skills are bundles of procedural knowledge that the agent loads on
demand. They follow the [Anthropic SKILL.md open standard][spec] (December
2025), which is also adopted by Claude Code, OpenCode, and ClawHub  -  so
Nymeria-installed skills are portable to those tools and vice versa.

[spec]: https://github.com/anthropics/skills

## What a skill is

A skill is a directory:

```
my-skill/
├── SKILL.md              REQUIRED: YAML frontmatter + markdown body
├── scripts/              optional: invoked via admin-enabled bash_execute
├── references/           optional: markdown files the agent reads on demand
└── assets/               optional: path-only, never auto-read
```

`SKILL.md` starts with YAML frontmatter and ends with a markdown body. The
accepted keys match Anthropic's strict spec:

```yaml
---
name: pdf                              # required, kebab-case
description: Extract text/tables…      # required, the routing signal
license: MIT                           # optional
allowed-tools: "Read,Write,Bash(pdftotext:*)"  # optional, advisory
metadata: {author: you@example.com}    # optional
---

# PDF skill
Step 1: …
```

A skill **does not register new Python tools** or Pydantic schemas. It is
text + an advisory whitelist over Nymeria's *existing* tools. The body typically
tells the agent to `file_read` a reference file or, when an admin has enabled it, `bash_execute` a script.

In the UI, **Skill** means instructions only. **Skill Kit** means a Skill that
also declares Nymeria tool dependencies in `metadata.nymeria.required_tools`.

## Skill Kits

A **Skill Kit** is a normal Agent Skill that also declares exact Nymeria tools
to bind when the agent activates it. This keeps the default tool list thin:
the agent first sees only the skill name/description, then `Skill(name=...)`
returns the instructions and temporarily binds the required tool schemas.

Use `metadata.nymeria.required_tools`; do not use portable `allowed-tools` for
Nymeria auto-binding:

```yaml
---
name: trigger-management
description: Configure, inspect, test, and troubleshoot Nymeria triggers.
allowed-tools: Read
metadata:
  nymeria:
    required_tools:
      - trigger_config
      - trigger_info
    tool_ttl: 2h
---
```

- `required_tools` must be exact Nymeria tool names. Categories, globs, and
  Anthropic-style `Bash(...)` patterns are not interpreted.
- `tool_ttl` accepts `Nm`, `Nh`, `Nd`, `Nw`, or `never`/`permanent`; default is `2h`.
- `internal: true` under `metadata.nymeria` keeps a skill available to code
  paths such as `/goal` while hiding it from user-facing `/skill`, `/kit`,
  and `/skills list` surfaces.
- `allowed-tools` remains advisory/portable and never auto-binds tools.
- Required tools already present in the current graph are treated as a no-op
  and listed in the activation result as skipped already-enabled tools.
- The activation result lists newly added/un-disabled required tools separately
  from tools skipped because they were already enabled.
- Binding is strict. If any required tool is unknown, unloadable, or blocked
  by the admin-only gate, activation fails and no tool config is mutated.
- Activation may remove required tools from `disabled_tools`, matching
  `tool_manage(action="enable")`; admin-only restrictions still apply.

If a Skill Kit binds a new tool, the current graph invocation ends with
`Command(goto=END)`, Nymeria rebuilds the graph, emits a `tool_reload` event
with `source="skill_kit"` and `skill_name`, then resumes the same user turn
with the new tools callable.

### Slash-command activation

User-facing skill activation uses fixed slash-command roots so skill names
cannot collide with built-in commands:

- `/skill <name>` activates a markdown-only skill on the current thread and
  sends its SKILL.md body into the current model turn.
- `/skill <name> <prompt>` does the same and appends the prompt under the skill
  body before the turn reaches the model. File and image attachments remain
  attached to the same turn.
- `/skill <name> off` deactivates a markdown-only skill on the current thread.
- `/kit <name>` activates a Skill Kit on the current thread using the kit's
  `tool_ttl`, binds its required tools, and sends the kit body into the current
  model turn.
- `/kit <name> <ttl>` activates it with a one-time TTL override such as `30m`,
  `1h`, `24h`, or `permanent`.
- `/kit <name> <ttl> <prompt>` uses the TTL override and appends the prompt
  under the kit body before the turn reaches the model.
- `/kit <name> <prompt>` uses the kit's default TTL and treats the tail as the
  prompt when the first tail token is not a valid TTL.
- `/kit <name> off` deactivates it on the current thread, removing the skill
  from `ThreadConfig.enabled_skills` and evicting the kit's temporary tools.
- `/skills` or `/skills list` lists visible skills and kits with
  active/inactive status for the current thread.
- `/skills show <name>` returns the installed skill body without activating it.
- `/skills off all` deactivates every visible active skill or kit on the
  current thread.

Skills or kits marked with:

```yaml
metadata:
  nymeria:
    internal: true
```

are not activatable through `/skill` or `/kit` and do not appear in normal
`/skills` listings, but remain inspectable through `/skills show <name>` for
debugging.

### Bundled self-improve skill and the focused management kits

`self-improve` ships as a bundled, text-only guidance skill: it binds no tools
(`required_tools` is empty). Its body holds the capability-expansion operating
philosophy plus the safety rules and a "which kit when" router. The
capability-expansion tools themselves live in four focused, bundled Skill Kits
that each bind their tools (ttl 2h) only when activated:

| Skill Kit | Binds | Use for |
|-----------|-------|---------|
| `tool-management` | `tool_search`, `tool_manage`, `tool_create`, `api_discover`, `http_request` | find/enable/build tools, including HTTP or API-backed custom tools |
| `skill-management` | `skill_manage`, `skill_write`, `skill_edit` | find/install/create/edit Skills and Skill Kits |
| `mcp-management` | `manage_mcp` | find/install/test/manage MCP servers (ships with the single `manage_mcp` tool today) |
| `credential-management` | `auth_inspect`, `auth_cleanup`, `auth_bindings`, `request_credential` | request/inspect/clean up credentials (these auth tools are in the default seed, so this kit is mainly guidance) |

Nymeria initializes `self-improve` and all four `*-management` kits in each user
profile's `enabled_global_skills` list once, so they are on by default for new
threads and their tools load lazily on activation. The Settings → Skills "Enable
globally" checkbox is the source of truth: unticking one removes it from the
user's default thread skill set and Nymeria will not silently re-add it.

Activate `Skill(name="self-improve")` for the operating philosophy, then the
matching kit for the work: inspect existing capabilities, enable
tools/skills/MCP servers when they already fit, discover/test APIs when needed,
create reusable HTTP or Python tools through `tool_create`, then write or edit
the workflow as a user-scope Skill Kit with `skill_write` or `skill_edit`.
Generated Skill Kits are enabled on the current thread by default through
`ThreadConfig.enabled_skills`; they are not added to `enabled_global_skills`
unless the user later enables them globally in Settings. Codebase
self-modification tools (`claude_code`, `reload_all`, and rollback) stay
separate and admin-only.

## Progressive disclosure (how the context budget stays small)

Three layers:

1. **Always loaded (per turn):** only the `(name, description)` pair of each
   active skill. These live inside the `Skill` meta-tool's *description*
   field. Budget: ~100 tokens per skill, 15k chars total. No skill content is
   ever injected into `soul.md` or the thread instructions.
2. **On activation:** when the agent calls `Skill(name=...)`, the full
   `SKILL.md` body is returned as a `ToolMessage`. Lives in conversation
   history only.
3. **On demand:** `references/*.md` load only if the skill's body tells the
   agent to `file_read` them. `scripts/*` run only if the body tells the agent
   to use an enabled tool such as `bash_execute` for them. `assets/` are never auto-read.

This is what lets a user keep dozens of skills installed without context
bloat  -  the agent pays tokens only for the skills it actually activates.

## Where skills live on disk

Four scope layers, in precedence order (name collisions: user > global > bundled):

| Scope | Location | Installed by |
|-------|----------|--------------|
| Thread-enabled | referenced by name in `ThreadConfig.enabled_skills` | per-thread toggle |
| User | `data/skills/users/<user_id>/<skill-name>/` | marketplace install, user scope |
| Global | `data/skills/global/<skill-name>/` | marketplace install, global scope (admin) |
| Bundled | `Nymeria/nymeria/skills_bundled/<skill-name>/` | ships with the repo |

`enabled_global_skills` on the user profile contains skills active by default
on every thread. Nymeria seeds new and unmigrated profiles with `self-improve`
plus the `tool-management`, `skill-management`, `mcp-management`, and
`credential-management` kits (`DEFAULT_GLOBAL_SKILLS` in
`nymeria/core/user_profile.py`), but after that the list is fully
user-controlled. Per-thread
`enabled_skills` extends that set; per-thread `disabled_skills` subtracts from
both global defaults and thread-local enables.

## REST API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/skills` | list installed skills (all scopes for user) |
| GET | `/skills/{name}` | full body + frontmatter |
| POST | `/skills/install` | install from marketplace (body: `{name, source, scope}`) |
| DELETE | `/skills/{name}?scope=user\|global` | uninstall |
| GET | `/skills/marketplace/search?source=anthropic&q=...` | search external registry |
| GET | `/threads/{id}/skills` | resolved active-skill set for this thread |
| GET/PUT | `/settings/global-skills` | get/set `enabled_global_skills` |

Plus: the existing `PATCH /threads/{id}/config` accepts `enabled_skills` and
`disabled_skills` fields.

Skill metadata responses include `required_tools`, `tool_ttl`,
`is_skill_kit`, and `default_active` in addition to the portable Agent Skills
fields.

Implementation: the Skills HTTP routes are mounted from
`nymeria/api/routers/skills.py`; runtime Skill Kit tool binding remains in the
`Skill` meta-tool and agent graph path.

## Agent-facing tools

Capability-expansion tools are optional and normally arrive through the focused
management kits, not through the default tool list. The skill tools below arrive
through `Skill(name="skill-management")`:

- `skill_manage(action='list'|'search'|'install'|'enable'|'disable'|'inspect'|'status'|'prune', ...)`
- `list_installed_skills(scope='all'|'user'|'global'|'bundled')`
- `search_skills(query, source='installed'|'anthropic', top_k=8)`
- `install_skill(name, source='anthropic', scope='user'|'global')`

`skill_write` and `skill_edit` are the preferred authoring facades exposed by
the `skill-management` kit; `skill_write` accepts full SKILL.md markdown plus
optional scripts, and `skill_edit` rewrites an existing SKILL.md while
preserving auxiliary files. `tool_create` (from the `tool-management` kit)
handles reusable HTTP or Python helper tools.

The progressively-disclosed `Skill(name)` meta-tool is *not* in `SEED_TOOLS`  -
it's synthesized per-graph in
`NymeriaAgent._build_skill_meta_tool()` and only appears on threads with ≥1
active skill.

## Semantic search

`search_skills` uses a three-stage fallback chain so natural-language intent
queries find the right skill even when the query and the skill name share no
keywords:

1. **Semantic**  -  OpenAI-compatible embeddings indexed in `sqlite-vec`,
   using `EMBEDDING_MODEL` (default `text-embedding-3-small`) and
   `EMBEDDING_API_KEY`. Returns top-k by cosine similarity on
   `name + description + allowed-tools`.
2. **BM25 / FTS5**  -  sqlite `FTS5` full-text search with Porter stemming,
   ranked by `bm25()`. No model, no network. Kicks in when semantic is
   unavailable or returns no results.
3. **Substring**  -  last-resort case-insensitive substring match over names
   and descriptions.

The tool's JSON response always includes a ``mode`` field (`"semantic"`,
`"bm25"`, or `"substring"`) plus a ``warning`` when running degraded:

```json
{
  "count": 3,
  "mode": "bm25",
  "warning": "semantic search unavailable (EMBEDDING_API_KEY not set; semantic search disabled); falling back to keyword search. Set EMBEDDING_API_KEY on the server for better skill discovery.",
  "results": [{"name": "pdf", "description": "…", "score": 0.71, "scope": "user"}]
}
```

The index is namespaced. Installed skills live in the `installed` namespace
and get rebuilt on every `SkillManager.reload()` (i.e. after an install,
uninstall, or reload-skills tool call). Marketplace entries live in
`marketplace:<source>` namespaces and are refreshed lazily with a 15-minute
TTL when the agent searches `source="anthropic"`.

Index storage: `data/skills/index.db` (sqlite). Zero new Python deps  -
`openai` is installed transitively by the OpenAI/LangChain dependencies, and
`sqlite-vec` is installed through `requirements-sqlite.txt`, which
`requirements.txt` includes for the default local backend.

### Marketplace fetch performance

The Anthropic marketplace fetcher downloads the entire `anthropics/skills`
repo as a single tarball (~few MB) via GitHub's codeload endpoint and
caches the bytes for 15 minutes. Both `list()` (parse all SKILL.md
frontmatter in-memory) and `fetch()` (extract a single skill's subtree)
share that cache  -  a typical "search → install" flow hits the network
exactly once.

## Marketplace

Phase 1 supports Anthropic's `anthropics/skills` repo only, fetched via the
GitHub Contents API. ClawHub (`openclaw.io`) and arbitrary git URLs are
stubbed with fixed interfaces  -  Phase 2 will wire them without any API change.

Marketplace list results are cached for 15 minutes per source. The install
path runs a light suspicious-pattern scan (`curl … | sh`, `rm -rf /`, fork
bombs) and logs warnings; it does not hard-block.

## Graph cache invalidation

`NymeriaAgent._get_memory_hash()` folds in a skills fingerprint (name, scope,
description hash, allowed_tools, and Skill Kit required tools/TTL for each active skill). Any of these
invalidate the per-(user, thread) graph cache:

- User toggles a skill in `enabled_global_skills`
- Thread flips `enabled_skills` or `disabled_skills`
- A `SKILL.md` description, allowed-tools, required tools, or Skill Kit TTL changes on disk
- A skill is installed or uninstalled via the REST endpoints

The body itself is re-read from disk at activation time, so edits to a
skill's body (not frontmatter) propagate immediately without a cache flush.

## Desktop UI

- **Settings → Skills**  -  install, uninstall, and toggle globally-enabled
  skills. "Browse Marketplace" opens a modal for searching
  `anthropics/skills`. Skill Kits show required-tool chips in their rows.
- **Thread Settings → Skills**  -  per-thread enable/disable of any installed
  skill, showing which are already active via the global default and which
  tools a Skill Kit will bind on activation.
- Skills panels force-refresh installed, global, and thread skill state on
  open. Agent-authored changes from `skill_write`, `skill_edit`,
  `skill_manage`, `install_skill`, and Skill Kit reload events also refresh
  the desktop stores, so a newly written Skill Kit appears without a full app
  reload.
- **Chat rendering**  -  when the agent fires `Skill(name=...)` the invocation
  renders as a distinguishable `SkillCard` (colored border, markdown-rendered
  body) rather than a generic `ToolCallCard`.

## Agent-authored skills

`skill_write` is the agent-facing write path for generated Skills and Skill
Kits. It accepts full SKILL.md markdown plus optional bundled scripts.
`skill_edit` renames or rewrites an existing generated Skill package while
preserving auxiliary files. Generated skills are user-scope by default; global
publish/edit requires admin. Both tools strictly validate Skill Kit
`required_tools` and refuse to publish a user skill that would shadow an
existing bundled/global skill.

When `activate_current_thread=true`, `skill_write`, `skill_edit`, and
`skill_manage(action="enable")` update `ThreadConfig.enabled_skills`, reload
the skill manager as needed, invalidate graph caches, and queue a same-turn
reload with `source="skill_write"`, `source="skill_edit"`, or
`source="skill_install"` and a matching reason. The emitted `tool_reload` may
carry an empty `tools` list because the refreshed capability is the `Skill`
meta-tool index, not a newly bound normal tool.

Marketplace installs or thread enables through `skill_manage` use
`source="skill_install"`.

## Security posture

- Skill bodies execute nothing themselves  -  they are instructions to the
  model. Actual side effects go through Nymeria's existing tools
  (`bash_execute`, `file_write`, etc.) which already honor the thread's
  enabled-tools and disabled-tools lists.
- Skill Kits can only bind tools that the same user could enable through
  `tool_manage(action="enable")`; invalid, unloadable, and admin-blocked
  dependencies fail strictly with no partial writes.
- `allowed-tools` frontmatter is advisory and portable. Nymeria only appends
  a missing-tool notice when an entry is also a known Nymeria tool name that
  is absent from the current thread; portable names like `Read`, `Write`, and
  `Bash(...)` do not warn.
- Marketplace installs land in `users/<user_id>/` by default; promoting to
  `global/` is an explicit UI action. A suspicious-pattern scan flags obvious
  red flags in SKILL.md and scripts.
- Bundled skills cannot be uninstalled at runtime (they're part of the repo).

## Future improvements

- **Local-server embeddings.** OpenAI-compatible `/v1/embeddings` endpoints
  can be used by pointing `EMBEDDING_BASE_URL` at the server and setting
  `EMBEDDING_API_KEY` to that server's accepted token. The current sqlite-vec
  schema expects 1536-dimensional vectors, so use a compatible model.
- **Agent-authored skill resources**  -  v1 writes only `SKILL.md`; future work
  can add validated reference files, assets, and scripts when a concrete use
  case needs them.
- **ClawHub + arbitrary git URL marketplaces**  -  the `marketplace.py`
  fetcher stubs are ready for Phase 2.
- **Skill version pinning and semver resolution**  -  currently always
  grabs `main`.
- **Graph RAG-Tool Fusion / hybrid retrieval**  -  combining semantic +
  BM25 with reciprocal rank fusion beats either alone past ~100 skills.
  Not worth the code until the installed library outgrows ~50 skills.

## References

- Anthropic spec + reference skills: https://github.com/anthropics/skills
- Skills module: `nymeria/skills/` (loader, meta-tool factory, marketplace, embedding index)
- Agent-facing tools: `nymeria/tools/search_skills.py`
- Desktop UI: `nymeria-desktop/src/lib/components/skills/`, `stores/skills.svelte.ts`
- [Anthropic `tool_search_with_embeddings` cookbook](https://github.com/anthropics/claude-cookbooks/blob/main/tool_use/tool_search_with_embeddings.ipynb)  -  canonical embedding-search pattern that this implementation mirrors (differs only in embedding model: we reuse Nymeria's existing `text-embedding-3-small` instead of `all-MiniLM-L6-v2` to avoid shipping a local model)
