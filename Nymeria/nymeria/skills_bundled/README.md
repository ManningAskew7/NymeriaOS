# skills_bundled/

Built-in SKILL.md bundles shipped with Nymeria. Each subdirectory contains a SKILL.md file.

## Contents

- `callable-thread-builder/`  -  skill for creating callable threads
- `cli-customization/`  -  kit: read/reconfigure the user's terminal CLI status bars via the `cli_statusbar_*` tools (not default-on; discovered via search or `/kit`)
- `credential-management/`  -  kit: request, inspect, and clean up credentials and connections
- `mcp-management/`  -  kit: find, install, test, and manage MCP servers
- `nymeria-resources/`  -  guidance skill: map of the on-disk resource stores and the rules for editing them directly as files (not default-on; pointed at by the file-tool descriptions and `data/README.md`)
- `orchestrate/`  -  internal kit: multi-thread orchestration
- `regression-noop/`  -  no-op skill used as a regression test fixture
- `self-improve/`  -  guidance skill: capability-expansion philosophy and routing to the kits below
- `skill-management/`  -  kit: find, install, create, and edit Skills and Skill Kits
- `tool-management/`  -  kit: find, enable, and build tools, including HTTP/API-backed ones
- `trigger-management/`  -  kit: inspect and manage event triggers
- `workflow-authoring/`  -  kit: author, test, and publish nym-SDK workflows (not default-on; discovered via search or `/kit`)

The `*-management` kits plus `self-improve` are the default-on capability set
(see `nymeria/core/user_profile.py` `DEFAULT_GLOBAL_SKILLS`). `self-improve` is a
text-only guidance skill that routes to the kits; the kits bind their tools only
when activated. Bundled skills are read-only at runtime; edit them here and commit.
User- and agent-authored skills live separately under the runtime data dir
(`data/skills/{global,users}/`), created via the `skill_write` tool.
