# Agent Skills

Agent Skills are bundles of procedural knowledge that the agent loads on
demand. They follow the [Anthropic SKILL.md open standard][spec] (December
2025), which is also adopted by Claude Code, OpenCode, and ClawHub — so
Nymeria-installed skills are portable to those tools and vice versa.

[spec]: https://github.com/anthropics/skills

## What a skill is

A skill is a directory:

```
my-skill/
├── SKILL.md              REQUIRED: YAML frontmatter + markdown body
├── scripts/              optional: invoked via the existing bash_execute tool
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
tells the agent to `file_read` a reference file or `bash_execute` a script.

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
   to `bash_execute` them. `assets/` are never auto-read.

This is what lets a user keep dozens of skills installed without context
bloat — the agent pays tokens only for the skills it actually activates.

## Where skills live on disk

Four scope layers, in precedence order (name collisions: user > global > bundled):

| Scope | Location | Installed by |
|-------|----------|--------------|
| Thread-enabled | referenced by name in `ThreadConfig.enabled_skills` | per-thread toggle |
| User | `data/skills/users/<user_id>/<skill-name>/` | marketplace install, user scope |
| Global | `data/skills/global/<skill-name>/` | marketplace install, global scope (admin) |
| Bundled | `Nymeria/nymeria/skills_bundled/<skill-name>/` | ships with the repo |

`enabled_global_skills` on the user profile picks which installed skills are
active-by-default on every thread. Per-thread `enabled_skills` extends that
set; per-thread `disabled_skills` subtracts from it.

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

## Agent-facing tools

Registered in `ALL_TOOLS` so the agent can manage its own skill library:

- `list_installed_skills(scope='all'|'user'|'global'|'bundled')`
- `search_skills(query, source='installed'|'anthropic')`
- `install_skill(name, source='anthropic', scope='user'|'global')`

The progressively-disclosed `Skill(name)` meta-tool is *not* in `ALL_TOOLS` —
it's synthesized per-graph in
`NymeriaAgent._build_skill_meta_tool()` and only appears on threads with ≥1
active skill.

## Marketplace

Phase 1 supports Anthropic's `anthropics/skills` repo only, fetched via the
GitHub Contents API. ClawHub (`openclaw.io`) and arbitrary git URLs are
stubbed with fixed interfaces — Phase 2 will wire them without any API change.

Marketplace list results are cached for 15 minutes per source. The install
path runs a light suspicious-pattern scan (`curl … | sh`, `rm -rf /`, fork
bombs) and logs warnings; it does not hard-block.

## Graph cache invalidation

`NymeriaAgent._get_memory_hash()` folds in a skills fingerprint (name + scope
+ description hash + allowed_tools for each active skill). Any of these
invalidate the per-(user, thread) graph cache:

- User toggles a skill in `enabled_global_skills`
- Thread flips `enabled_skills` or `disabled_skills`
- A `SKILL.md` description / allowed-tools changes on disk
- A skill is installed or uninstalled via the REST endpoints

The body itself is re-read from disk at activation time, so edits to a
skill's body (not frontmatter) propagate immediately without a cache flush.

## Desktop UI

- **Settings → Skills** — install, uninstall, and toggle globally-enabled
  skills. "Browse Marketplace" opens a modal for searching
  `anthropics/skills`.
- **Thread Settings → Skills** — per-thread enable/disable of any installed
  skill, showing which are already active via the global default.
- **Chat rendering** — when the agent fires `Skill(name=...)` the invocation
  renders as a distinguishable `SkillCard` (colored border, markdown-rendered
  body) rather than a generic `ToolCallCard`.

## Phase 1.5 — agent-authored skills (not yet implemented)

The architecture already admits skill-authoring tools:
`create_skill(name, description, body, scope, allowed_tools)`,
`edit_skill(name, ...)`, `delete_skill(name, scope)`. Until those ship, the
agent can still author skills via the installed `skill-creator` skill plus
`file_write` + the existing uninstall endpoint; the dedicated tools are a
correctness/ergonomics upgrade (Pydantic-validated frontmatter, atomic
`SkillManager.reload()`).

## Security posture

- Skill bodies execute nothing themselves — they are instructions to the
  model. Actual side effects go through Nymeria's existing tools
  (`bash_execute`, `file_write`, etc.) which already honor the thread's
  enabled-tools and disabled-tools lists.
- `allowed-tools` frontmatter is cross-checked at activation: if the skill
  names a tool the thread has disabled, a notice appended to the returned
  body asks the agent to request user permission before proceeding.
- Marketplace installs land in `users/<user_id>/` by default; promoting to
  `global/` is an explicit UI action. A suspicious-pattern scan flags obvious
  red flags in SKILL.md and scripts.
- Bundled skills cannot be uninstalled at runtime (they're part of the repo).

## References

- Research synthesis on skills architectures:
  `compass_artifact_wf-e088516e-e35b-4ba3-8124-078f934fdccd_text_markdown.md`
- Anthropic spec + reference skills: https://github.com/anthropics/skills
- Skills module: `nymeria/skills/` (loader, meta-tool factory, marketplace)
- Agent-facing tools: `nymeria/tools/search_skills.py`
- Desktop UI: `nymeria-desktop/src/lib/components/skills/`, `stores/skills.svelte.ts`
