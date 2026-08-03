"""Built-in slash-command catalog for :class:`CommandService`.

Extracted from ``command_service.py::CommandService._register_defaults`` (slice
03 F3) so the ~730-line command catalog lives apart from the registry and
dispatch mechanism. This module is the single declarative source of the
built-in slash commands; ``CommandService._register_defaults`` is now a thin
wrapper that calls :func:`register_default_commands`.

Each ``service.register(...)`` call is the verbatim declaration moved from the
former method body (no behavior change). ``service`` is passed (rather than the
bound ``register`` callable) so every declaration is still statically checked
against the full typed ``register`` signature. The module imports
``CommandService`` only under ``TYPE_CHECKING``, so it stays a runtime leaf with
no import cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .command_executor_llm import THINK_VALUES
from .command_params import CommandParam

if TYPE_CHECKING:
    from .command_service import CommandService


# ``/branch`` is a pure delegate to ``/thread branch``, so the two share one
# declaration: a divergence would generate two different usage strings for one
# handler.
_BRANCH_PARAMS = (
    CommandParam(
        "from",
        kind="option",
        type="int",
        aliases=("-f",),
        description="Branch from this message index (1-based)",
    ),
    CommandParam("title", kind="rest", description="Title for the new branch"),
)


# Every hook verb that names one hook takes the same argument: an id or a
# unique prefix of one (``_resolve_hook`` does the prefix match). One
# declaration keeps those usage strings from drifting apart.
_HOOK_ID_PARAM = CommandParam(
    "id", required=True, description="Hook id or unique id prefix"
)

# ``/hook create`` and ``/hook edit`` collect conditions, rewrites, and the
# fire gate identically, and hand the collected strings to the same semantic
# parsers, so the two declarations share one tuple.
_HOOK_CONDITION_PARAMS = (
    CommandParam(
        "cond",
        kind="option",
        repeatable=True,
        label='"f op v"',
        description="Match condition, quoted: field operator value",
    ),
    CommandParam(
        "set",
        kind="option",
        repeatable=True,
        label="arg=value",
        description="Argument rewrite for rewrite_arg",
    ),
    CommandParam(
        "fire_cond",
        kind="option",
        repeatable=True,
        label='"f op v"',
        description="Definition-level fire gate, quoted like --cond",
    ),
    CommandParam(
        "case_sensitive",
        kind="flag",
        type="bool",
        description="Compare condition values case-sensitively",
    ),
)

# ``/hook approve`` and ``/hook deny`` are one handler with a boolean, so they
# share one declaration. The note is a rest param, which also means an
# apostrophe in it is ordinary English rather than an unbalanced quote.
_HOOK_APPROVAL_PARAMS = (
    CommandParam(
        "id",
        required=True,
        label="approval-id",
        description="Pending approval id or unique prefix",
    ),
    CommandParam(
        "note", kind="rest", description="Note recorded with the decision"
    ),
)

# ``/skills enable`` and ``/skills disable`` are one handler with a boolean, so
# they share one declaration. The flag comes first so the generated usage keeps
# advertising ``[--global] <name>``.
_SKILL_STATE_PARAMS = (
    CommandParam(
        "global",
        kind="flag",
        type="bool",
        description="Apply to every thread instead of only this one",
    ),
    CommandParam(
        "name",
        required=True,
        choices_ref="skills",
        description="Installed skill name",
    ),
)

# Every ``/mcp`` verb that names one server takes the same argument and resolves
# it through the same registry lookup, so one declaration keeps those usage
# strings from drifting apart.
_MCP_SERVER_ID_PARAM = CommandParam(
    "server_id",
    required=True,
    label="server-id",
    description="Configured MCP server id",
)

# ``/triggers enable``, ``disable``, and ``delete`` each name one trigger.
_TRIGGER_ID_PARAM = CommandParam(
    "trigger_id",
    required=True,
    label="trigger-id",
    description="Event trigger id",
)


def register_default_commands(service: "CommandService") -> None:
    """Register every built-in slash command on ``service`` in catalog order.

    Order matters only for diff stability and the F7 executor-binding guard
    test's parametrize ids; the registry sorts on read (``list_commands``) and
    ``validate_registry`` is order-independent.
    """
    # Function-local to dodge the module cycle (command_service imports this
    # module at top level).
    from .command_service import CHAT_PLATFORM_SURFACES

    service.register(
        "help",
        description="Show available commands",
        category="General",
        usage="/help [command|all]",
        aliases=("h",),
        examples=("/help provider", "/help all"),
    )
    service.register(
        "status",
        description="Model, context, tools, and task summary",
        category="Status",
        requires_thread=True,
    )
    service.register(
        "thread",
        description="Show active thread context usage",
        category="Status",
        aliases=("threads", "t"),
        requires_thread=True,
        params=(),
    )
    # /thread management subtree. The read verbs are agent-allowed; the
    # navigation, mutation, and destructive verbs are not (the agent has its
    # own thread tools, e.g. spawn_thread). Category "Thread" groups these with
    # the other thread-lifecycle commands (compact/clear/prune/notepad); it
    # also sorts after "Status" so the executable `thread` root registers
    # before its subs in the CLI backend-command proxy.
    service.register(
        "thread list",
        description="List threads",
        category="Thread",
        aliases=("thread_list",),
        params=(),
    )
    service.register(
        "thread switch",
        description="Switch the active thread",
        category="Thread",
        aliases=("thread_switch", "thread s"),
        agent_allowed=False,
        params=(
            CommandParam(
                "id_or_title",
                kind="rest",
                required=True,
                label="id-or-title",
                description="Thread id, id prefix, or title",
            ),
        ),
    )
    service.register(
        "thread new",
        description="Create a new thread and switch to it",
        category="Thread",
        aliases=("thread_new", "thread n"),
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
        params=(
            CommandParam(
                "title", kind="rest", description="Title for the new thread"
            ),
        ),
    )
    service.register(
        "thread delete",
        description="Delete a thread permanently",
        category="Thread",
        aliases=("thread_delete", "thread del", "thread rm"),
        mutates_state=True,
        danger_level="dangerous",
        agent_allowed=False,
        params=(
            CommandParam(
                "id",
                required=True,
                description="Thread id, id prefix, or title",
            ),
            CommandParam(
                "yes",
                kind="flag",
                type="bool",
                aliases=("-y",),
                description="Accepted for muscle memory; no prompt is shown",
            ),
        ),
    )
    service.register(
        "thread info",
        description="Show details for the active thread",
        category="Thread",
        aliases=("thread_info",),
        requires_thread=True,
        params=(),
    )
    service.register(
        "thread rename",
        description="Rename the active thread",
        category="Thread",
        aliases=("thread_rename",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
        params=(
            CommandParam(
                "title",
                kind="rest",
                required=True,
                description="New title for the active thread",
            ),
        ),
    )
    service.register(
        "thread pin",
        description="Pin or unpin a thread",
        category="Thread",
        aliases=("thread_pin",),
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
        # `state` carries no choices: the handler also accepts the
        # yes/no/true/false synonyms, so a declared choice set would be
        # narrower than what actually works. The label keeps the usage
        # string advertising the canonical trio.
        params=(
            CommandParam(
                "id",
                description="Thread to pin (defaults to the active thread)",
            ),
            CommandParam(
                "state",
                label="on|off|toggle",
                description="on, off, or toggle (default toggle)",
            ),
        ),
    )
    service.register(
        "thread config",
        description="Show the active thread's configuration",
        category="Thread",
        aliases=("thread_config",),
        requires_thread=True,
        params=(),
    )
    service.register(
        "thread branch",
        description="Branch the active thread into a new thread",
        category="Thread",
        aliases=("thread_branch", "thread fork"),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
        params=_BRANCH_PARAMS,
    )
    service.register(
        "thread compact",
        description="Compact the active thread's context",
        category="Thread",
        aliases=("thread_compact",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
        params=(
            CommandParam(
                "yes",
                kind="flag",
                type="bool",
                aliases=("-y",),
                description="Accepted for muscle memory; no prompt is shown",
            ),
        ),
    )
    service.register(
        "branch",
        description="Branch the active thread into a new thread",
        category="Thread",
        aliases=("fork",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
        # Same declaration as "thread branch": /branch is a pure delegate, so
        # both usages must generate identically.
        params=_BRANCH_PARAMS,
    )
    # /team read commands (backlog #100 phase 2). Read-only sugar over the
    # shared team serializer; mutations go through the team_manage tool, the
    # teams REST API, or the desktop UI.
    service.register(
        "team",
        description="List your callable-thread teams",
        category="Thread",
        usage="/team [list|show <team>]",
        aliases=("teams",),
    )
    service.register(
        "team list",
        description="List your callable-thread teams",
        category="Thread",
        aliases=("team_list",),
        params=(),
    )
    service.register(
        "team show",
        description="Show one team's members and description",
        category="Thread",
        usage="/team show <team-id-or-name>",
        aliases=("team_show",),
    )
    service.register(
        "context",
        description="Detailed context and tool breakdown",
        category="Status",
        requires_thread=True,
    )
    service.register(
        "usage",
        description="Show token usage and cost statistics",
        category="Status",
        usage="/usage [session]",
        aliases=("tokens", "cost"),
        requires_thread=True,
    )
    service.register(
        "usage session",
        description="Show session-wide (cumulative) token usage for this thread",
        category="Status",
        usage="/usage session",
        aliases=("usage_session",),
        requires_thread=True,
    )
    service.register(
        "tasks",
        description="Scheduled tasks overview",
        category="TODOs",
        usage="/tasks [active|pending|in_progress|done|all]",
    )
    service.register(
        "model",
        description="Show or change the model",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        examples=("/model claude-fable-5", "/model gpt-5.5 thread"),
        # ``--force`` is advertised BEFORE the scope because the trailing
        # scope token is popped from the end of the argument list: a flag
        # typed after "global" would strand the scope word as an extra
        # positional. The name is checked against the live model list in the
        # handler (dynamic set, hence choices_ref rather than choices).
        params=(
            CommandParam(
                "name",
                choices_ref="models",
                description="Model id to switch to",
            ),
            CommandParam(
                "force",
                kind="flag",
                type="bool",
                description="Accept a model the provider does not list",
            ),
            CommandParam(
                "scope",
                kind="scope",
                description="Write the global default or this thread's override",
            ),
        ),
    )
    service.register(
        "models",
        description="List available provider models",
        category="LLM",
        params=(),
    )
    service.register(
        "fast",
        description="Switch this thread to the fast model tier",
        category="LLM",
        usage="/fast [on|off|set <model-id>]",
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "smart",
        description="Switch this thread to the smart model tier",
        category="LLM",
        usage="/smart [on|off|set <model-id>]",
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "background",
        description="Show or set the global background/utility model tier",
        category="LLM",
        usage="/background [set <model-id> | set-url <base-url> | clear]",
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "fallback",
        description="Manage the model fallback chain, active holds, and consent prompts",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        params=(),
    )
    # Chain verbs (backlog #129). They used to be tokens parsed by the parent
    # handler; registering them gives each verb a declared argument schema and
    # lets longest-prefix dispatch route it. Bare "/fallback" keeps its old
    # default of listing the chain, and the verbs stay agent-allowed (only the
    # CONSENT children below are human-only).
    service.register(
        "fallback list",
        description="List the primary model and the configured fallback chain",
        category="LLM",
        params=(),
    )
    service.register(
        "fallback add",
        description="Add a model to the fallback chain",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        params=(
            CommandParam(
                "model",
                required=True,
                label="model-id",
                choices_ref="models",
                description="Model id to add",
            ),
            CommandParam(
                "position",
                kind="option",
                type="int",
                description="1-based position in the chain (default: append)",
            ),
        ),
    )
    service.register(
        "fallback remove",
        description="Remove a model from the fallback chain",
        category="LLM",
        aliases=("fallback rm",),
        mutates_state=True,
        danger_level="normal",
        params=(
            CommandParam(
                "model",
                required=True,
                label="model-id",
                choices_ref="models",
                description="Model id to remove",
            ),
        ),
    )
    service.register(
        "fallback set",
        description="Replace the fallback chain with the given models, in order",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        params=(
            CommandParam(
                "models",
                required=True,
                repeatable=True,
                label="model-id",
                choices_ref="models",
                description="Model ids, in fallback order",
            ),
        ),
    )
    service.register(
        "fallback clear",
        description="Clear the fallback chain",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        params=(),
    )
    # Consent-family children (hook-family idiom). Registration makes the
    # registry's longest-prefix match dispatch "/fallback <sub>" straight to
    # the _cmd_fallback_<sub> executor handlers; the parent handler keeps
    # only the chain grammar. agent_allowed=False is load-bearing and
    # enforced pre-dispatch by CommandService.execute: the agent must not
    # resolve or revert its own model-swap consent.
    _fallback_sub_surfaces = ("desktop", "mobile", "cli", "api", "agent")
    service.register(
        "fallback status",
        description="Show the consent modes and this thread's active fallback hold",
        category="LLM",
        surfaces=_fallback_sub_surfaces,
        params=(),
    )
    service.register(
        "fallback revert",
        description="End the active thread's fallback hold and return to the primary model",
        category="LLM",
        surfaces=_fallback_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
        params=(),
    )
    service.register(
        "fallback approvals",
        description="List turns parked on a model-swap consent prompt",
        category="LLM",
        surfaces=_fallback_sub_surfaces,
        agent_allowed=False,
        params=(),
    )
    service.register(
        "fallback approve",
        description="Approve a parked model swap (optional hold in minutes or 'permanent')",
        category="LLM",
        usage="/fallback approve <id> [minutes|permanent] [note]",
        surfaces=_fallback_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
    )
    service.register(
        "fallback deny",
        description="Decline a parked model swap (the turn proceeds on the original model's outcome)",
        category="LLM",
        usage="/fallback deny <id> [note]",
        surfaces=_fallback_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
    )
    service.register(
        "think",
        description="Show or change thinking mode (thread-scoped when a thread is active)",
        category="LLM",
        aliases=("reasoning", "thinking"),
        mutates_state=True,
        danger_level="normal",
        examples=("/think high", "/think off global"),
        # THINK_VALUES is the handler's own value space (on/off plus the
        # effort ladder), so the dispatcher's choice set cannot drift from
        # the levels the picker offers.
        params=(
            CommandParam(
                "mode",
                choices=THINK_VALUES,
                description="Thinking state or reasoning effort level",
            ),
            CommandParam(
                "scope",
                kind="scope",
                description="Write the global default or this thread's override",
            ),
        ),
    )
    service.register(
        "provider",
        description="Show the active LLM provider, or browse one provider's actions",
        category="LLM",
        examples=("/provider anthropic", "/provider switch anthropic thread"),
        # One optional token, a provider id, is all the binder enforces:
        # registered subcommands never reach it (longest-prefix dispatch
        # routes them first). The label keeps advertising them, since the
        # usage line is where they are discovered.
        params=(
            CommandParam(
                "provider",
                choices_ref="providers",
                label=(
                    "<provider>|setup|list|set|switch|test|cliproxy"
                    "|reasoning-passback"
                ),
                description="Provider id to browse",
            ),
        ),
    )
    # No chat-bot surfaces and no agent: the typed fallback path is
    # "/provider setup key <secret>", which on a chat platform would persist
    # the key in the platform's message history. ``surfaces`` only hides the
    # command from menus, so ``blocked_surfaces`` ENFORCES the refusal at
    # execute() (the generic bot passthroughs forward any typed command).
    service.register(
        "provider setup",
        description=(
            "Guided provider configuration: key, connection, model, test, apply"
        ),
        category="LLM",
        usage="/provider setup <provider>",
        aliases=("provider_setup",),
        surfaces=("desktop", "mobile", "cli", "api"),
        blocked_surfaces=CHAT_PLATFORM_SURFACES,
        blocked_reason=(
            "It prompts for an API key, and anything typed on a chat surface "
            "persists in the platform's message history."
        ),
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
        agent_allowed=False,
    )
    # Same surface restriction as "provider setup": OAuth authorization
    # codes ride the typed "/provider cliproxy paste <url>" path, which on
    # a chat platform would persist them in the platform's message history.
    service.register(
        "provider cliproxy",
        description="CLIProxy subscription OAuth: login and route apply",
        category="LLM",
        usage=(
            "/provider cliproxy [target|login|use|relogin|paste|check"
            "|model|apply|cancel]"
        ),
        aliases=("provider_cliproxy",),
        surfaces=("desktop", "mobile", "cli", "api"),
        blocked_surfaces=CHAT_PLATFORM_SURFACES,
        blocked_reason=(
            "It handles OAuth authorization URLs, and anything typed on a "
            "chat surface persists in the platform's message history."
        ),
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
        agent_allowed=False,
    )
    service.register(
        "provider list",
        description="List LLM providers grouped by support tier",
        category="LLM",
        aliases=("provider_list",),
        params=(),
    )
    service.register(
        "provider set",
        description="Apply provider credentials to backend settings",
        category="LLM",
        aliases=("provider_set",),
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
        # no_echo on the credential fields: a mistyped invocation pastes a
        # bare secret where a key=value pair was expected, and the rejected
        # token must not come back in the error copy.
        params=(
            CommandParam(
                "provider",
                required=True,
                choices_ref="providers",
                description="Provider whose credentials to write",
            ),
            CommandParam(
                "values",
                required=True,
                repeatable=True,
                no_echo=True,
                label="key=value",
                description="Credential fields, e.g. api_key=<key>",
            ),
        ),
    )
    service.register(
        "provider test",
        description="Test provider connectivity without saving anything",
        category="LLM",
        aliases=("provider_test",),
        requires_admin=True,
        params=(
            CommandParam(
                "provider",
                choices_ref="providers",
                description="Provider to test (default: the active one)",
            ),
        ),
    )
    service.register(
        "provider switch",
        description="Switch the active LLM provider globally or for this thread",
        category="LLM",
        aliases=("provider_switch",),
        # No requires_admin: the gate is per-scope in the handler (global
        # needs admin, thread scope is any user's own override; the /model
        # shape). Backlog #138 tracks per-user provider config.
        mutates_state=True,
        danger_level="normal",
        note="Global scope is admin-only; thread scope is any user's own override.",
        examples=(
            "/provider switch openrouter",
            "/provider switch anthropic thread",
        ),
        # The provider set is dynamic (the llm_providers registry, aliases
        # included), so the spec lookup stays in the handler and the
        # declaration only advertises the reference.
        params=(
            CommandParam(
                "provider",
                required=True,
                choices_ref="providers",
                description="Provider id to switch to",
            ),
            CommandParam(
                "scope",
                kind="scope",
                default="global",
                description="Switch globally (default) or for this thread",
            ),
        ),
    )
    service.register(
        "provider reasoning-passback",
        description="Show whether prior-turn reasoning is replayed to the model",
        category="LLM",
        aliases=("provider_reasoning_passback", "provider passback"),
        params=(),
    )
    service.register(
        "config show",
        description="Show server settings",
        category="Settings",
        aliases=("config_show",),
    )
    service.register(
        "config get",
        description="Show one server setting",
        category="Settings",
        usage="/config get <key>",
        aliases=("config_get",),
    )
    service.register(
        "config set",
        description="Change a server setting",
        category="Settings",
        usage="/config set <key> <value>",
        aliases=("config_set",),
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
    )
    service.register(
        "settings",
        description="Show or change server settings (delegates to /config)",
        category="Settings",
        usage="/settings [show|get <key>|set <key> <value>]",
        mutates_state=True,
        danger_level="normal",
        note=(
            "The set branch requires an admin user, enforced at the update "
            "surface (CommandBackendClient.update_settings and PATCH /settings)."
        ),
    )
    service.register(
        "env show",
        description="Show environment variables",
        category="Settings",
        aliases=("env_show",),
        requires_admin=True,
    )
    service.register(
        "env get",
        description="Show one unmasked environment variable",
        category="Settings",
        usage="/env get <key>",
        aliases=("env_get",),
        requires_admin=True,
    )
    service.register(
        "env set",
        description="Change an environment variable",
        category="Settings",
        usage="/env set <key> <value>",
        aliases=("env_set",),
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
    )
    service.register(
        "tools core",
        description="Show core tools",
        category="Tools",
        aliases=("tools_core", "tools list_core"),
    )
    service.register(
        "tools optional",
        description="Show optional tools",
        category="Tools",
        aliases=("tools_optional",),
        requires_thread=True,
    )
    service.register(
        "tools enabled",
        description="Show enabled tools for this thread",
        category="Tools",
        aliases=("tools_enabled",),
        requires_thread=True,
    )
    service.register(
        "tools category",
        description="Show tools in a category",
        category="Tools",
        usage="/tools category <name>",
        aliases=("tools_category",),
        requires_thread=True,
    )
    service.register(
        "tools enable",
        description="Enable a tool or category on this thread",
        category="Tools",
        usage="/tools enable <tool_or_category>",
        aliases=("tools_enable",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        examples=("/tools enable web_search", "/tools enable productivity"),
    )
    service.register(
        "tools disable",
        description="Disable a tool or category on this thread",
        category="Tools",
        usage="/tools disable <tool_or_category>",
        aliases=("tools_disable",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "sequential-tools",
        description="Show or set sequential (ordered, one-at-a-time) tool execution",
        category="Tools",
        usage="/sequential-tools [on|off|inherit|global on|off]",
        mutates_state=True,
        danger_level="normal",
        # Deterministic operator setting, not something the model should flip; the
        # agent already has run_tools_in_order for per-batch ordering.
        agent_allowed=False,
    )
    service.register(
        "skill",
        description="Use a markdown-only skill with an optional prompt",
        category="Skills",
        usage="/skill <name> [prompt]",
        requires_thread=True,
        mutates_state=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "kit",
        description="Use a Skill Kit with optional TTL and prompt",
        category="Skills",
        usage="/kit <name> [ttl] [prompt]",
        requires_thread=True,
        mutates_state=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "skills",
        description="List skills visible on this thread",
        category="Skills",
        requires_thread=True,
        examples=("/skills show summarize", "/skills enable --global summarize"),
        # Bare "/skills" lists. Registered subcommands never reach this handler
        # (longest-prefix dispatch routes them first), so the only token the
        # binder sees here is a typo; the label keeps advertising the family,
        # since the usage line is where the verbs are discovered.
        params=(
            CommandParam(
                "subcommand",
                label="list|show <name>|off all",
                description="Skills subcommand (bare /skills lists)",
            ),
        ),
    )
    service.register(
        "skills list",
        description="List skills visible on this thread",
        category="Skills",
        aliases=("skills_list",),
        requires_thread=True,
        params=(),
    )
    service.register(
        "skills show",
        description="Show a skill's markdown body without activating it",
        category="Skills",
        aliases=("skills_show",),
        params=(
            CommandParam(
                "name",
                required=True,
                choices_ref="skills",
                description="Installed skill name",
            ),
        ),
    )
    service.register(
        "skills off all",
        description="Deactivate every visible skill active on this thread",
        category="Skills",
        aliases=("skills_off_all",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        params=(),
    )
    service.register(
        "skills search",
        description="Search a skills marketplace for installable skills",
        category="Skills",
        aliases=("skills_search",),
        # The query is a repeatable POSITIONAL, not a rest tail: the hand
        # parser pulled --source out from any position and joined what was
        # left, so "/skills search creator --source clawhub" must keep working.
        params=(
            CommandParam(
                "query",
                repeatable=True,
                description="Words to match against marketplace skill names",
            ),
            CommandParam(
                "source",
                kind="option",
                default="anthropic",
                description="Marketplace source to search",
            ),
        ),
    )
    service.register(
        "skills install",
        description="Install a skill from a marketplace",
        category="Skills",
        aliases=("skills_install",),
        mutates_state=True,
        danger_level="normal",
        # The marketplace source set lives in skills/marketplace.py and its
        # errors are the live-data validation, so --source stays an open string;
        # --scope is a closed pair the handler already checked by equality.
        params=(
            CommandParam(
                "name",
                required=True,
                choices_ref="skills",
                description="Marketplace skill name to install",
            ),
            CommandParam(
                "source",
                kind="option",
                default="anthropic",
                description="Marketplace source to install from",
            ),
            CommandParam(
                "scope",
                kind="option",
                choices=("user", "global"),
                default="user",
                description="Install for this user or for everyone",
            ),
        ),
    )
    service.register(
        "skills enable",
        description="Enable a skill on this thread (default) or globally",
        category="Skills",
        aliases=("skills_enable",),
        mutates_state=True,
        params=_SKILL_STATE_PARAMS,
    )
    service.register(
        "skills disable",
        description="Disable a skill on this thread (default) or globally",
        category="Skills",
        aliases=("skills_disable",),
        mutates_state=True,
        params=_SKILL_STATE_PARAMS,
    )
    service.register(
        "skills inspect",
        description="Show full skill details (metadata, scope, tools, references)",
        category="Skills",
        aliases=("skills_inspect",),
        params=(
            CommandParam(
                "name",
                required=True,
                choices_ref="skills",
                description="Installed skill name",
            ),
        ),
    )
    service.register(
        "mcp",
        description="MCP server management commands",
        category="MCP",
        requires_admin=True,
        # Bare "/mcp" lists; registered verbs are routed before this handler.
        params=(
            CommandParam(
                "subcommand",
                label="list|status|logs|discover|test|remove|retry",
                description="MCP subcommand (bare /mcp lists)",
            ),
        ),
    )
    service.register(
        "mcp list",
        description="List configured MCP servers",
        category="MCP",
        aliases=("mcp_list",),
        requires_admin=True,
        params=(),
    )
    service.register(
        "mcp status",
        description="Show MCP server status, with errors if any",
        category="MCP",
        aliases=("mcp_status",),
        requires_admin=True,
        params=(
            CommandParam(
                "server_id",
                label="server-id",
                description="Configured MCP server id (omit for every server)",
            ),
        ),
    )
    service.register(
        "mcp logs",
        description="Show install logs for an MCP server",
        category="MCP",
        aliases=("mcp_logs",),
        requires_admin=True,
        params=(
            _MCP_SERVER_ID_PARAM,
            CommandParam(
                "limit",
                type="int",
                default=20,
                description="How many trailing log lines to show",
            ),
        ),
    )
    service.register(
        "mcp discover",
        description="Force tool rediscovery for an MCP server",
        category="MCP",
        aliases=("mcp_discover",),
        requires_admin=True,
        mutates_state=True,
        params=(_MCP_SERVER_ID_PARAM,),
    )
    service.register(
        "mcp test",
        description="Test connectivity to an MCP server",
        category="MCP",
        aliases=("mcp_test",),
        requires_admin=True,
        params=(_MCP_SERVER_ID_PARAM,),
    )
    service.register(
        "mcp remove",
        description="Remove an MCP server and its tools",
        category="MCP",
        aliases=("mcp_remove", "mcp_rm", "mcp_delete"),
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
        params=(_MCP_SERVER_ID_PARAM,),
    )
    service.register(
        "mcp retry",
        description="Retry setup for a draft or failed MCP server",
        category="MCP",
        aliases=("mcp_retry",),
        requires_admin=True,
        mutates_state=True,
        danger_level="normal",
        params=(_MCP_SERVER_ID_PARAM,),
    )
    service.register(
        "triggers",
        description="Event-trigger automation commands",
        category="Automation",
        # Bare "/triggers" lists; registered verbs are routed before this one.
        params=(
            CommandParam(
                "subcommand",
                label="list|enable|disable|delete|history",
                description="Triggers subcommand (bare /triggers lists)",
            ),
        ),
    )
    service.register(
        "triggers list",
        description="List configured event triggers",
        category="Automation",
        aliases=("triggers_list",),
        params=(
            CommandParam(
                "enabled_only",
                kind="flag",
                type="bool",
                description="Hide disabled triggers",
            ),
            CommandParam(
                "thread",
                kind="option",
                label="id|current",
                description="Only triggers bound to this thread (`current` for the active one)",
            ),
        ),
    )
    service.register(
        "triggers enable",
        description="Enable an event trigger",
        category="Automation",
        aliases=("triggers_enable",),
        mutates_state=True,
        params=(_TRIGGER_ID_PARAM,),
    )
    service.register(
        "triggers disable",
        description="Disable an event trigger",
        category="Automation",
        aliases=("triggers_disable",),
        mutates_state=True,
        params=(_TRIGGER_ID_PARAM,),
    )
    service.register(
        "triggers delete",
        description="Delete an event trigger permanently",
        category="Automation",
        aliases=("triggers_delete",),
        mutates_state=True,
        danger_level="dangerous",
        params=(_TRIGGER_ID_PARAM,),
    )
    service.register(
        "triggers history",
        description="Show recent trigger execution history",
        category="Automation",
        aliases=("triggers_history",),
        params=(
            CommandParam(
                "trigger_id",
                label="trigger-id",
                description="Only this trigger's executions",
            ),
            CommandParam(
                "limit",
                kind="option",
                type="int",
                default=20,
                description="How many executions to show",
            ),
        ),
    )
    # Lifecycle hooks. The bare `/hook` is visible everywhere so a chat bot's
    # command menu shows one row; the multi-token subcommands are hidden from
    # chat surfaces to avoid dead `/hook_create`-style menu entries (execute()
    # ignores surface, so a bot forwarding `/hook create ...` still runs).
    # Desktop/mobile/CLI keep full subcommand autocomplete.
    _hook_sub_surfaces = ("desktop", "mobile", "cli", "api", "agent")
    service.register(
        "hook",
        description="Lifecycle-hook authoring and approval commands",
        category="Automation",
        aliases=("hooks",),
        # Bare "/hook" lists. Registered subcommands never reach the root
        # (longest-prefix dispatch routes them first), so the only tokens the
        # binder sees here are the unregistered "detail" synonym of show and
        # typos; the label keeps advertising the family, since the usage line
        # is where the verbs are discovered.
        params=(
            CommandParam(
                "subcommand",
                label=(
                    "list|create|show|edit|enable|disable|delete|test|log"
                    "|templates|install|approvals|approve|deny"
                ),
                description="Hook subcommand (bare /hook lists)",
            ),
            CommandParam(
                "id", description="Hook id, for the `detail` synonym of show"
            ),
        ),
    )
    service.register(
        "hook list",
        description="List lifecycle hooks",
        category="Automation",
        aliases=("hook_list",),
        surfaces=_hook_sub_surfaces,
        params=(
            CommandParam(
                "thread",
                kind="option",
                label="id|current",
                description="Show hooks bound to this thread (`current` for the active one)",
            ),
            CommandParam(
                "global",
                kind="flag",
                type="bool",
                description="Show only global hooks",
            ),
            CommandParam(
                "enabled_only",
                kind="flag",
                type="bool",
                description="Hide disabled hooks",
            ),
        ),
    )
    service.register(
        "hook show",
        description="Show one hook's full configuration",
        category="Automation",
        aliases=("hook_show", "hook_detail"),
        surfaces=_hook_sub_surfaces,
        params=(_HOOK_ID_PARAM,),
    )
    service.register(
        "hook create",
        description="Create a lifecycle hook",
        category="Automation",
        aliases=("hook_create",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
        examples=(
            '/hook create greet --event user_prompt_submit --action inject_text --text "Be brief."',
        ),
        # The name is the bare-word remainder, exactly as the hand parser
        # read it: options are pulled out from any position and what is left
        # joins, so "/hook create Block rm -rf --event ..." still names the
        # hook "Block rm -rf".
        params=(
            CommandParam(
                "name",
                required=True,
                repeatable=True,
                description="Hook name (bare words join)",
            ),
            CommandParam(
                "event",
                kind="option",
                required=True,
                description="Lifecycle event to hook",
            ),
            CommandParam(
                "action",
                kind="option",
                default="inject_context",
                description="What the hook does when it fires",
            ),
            CommandParam(
                "text",
                kind="option",
                description="Injected, notify, todo, or approval-prompt text",
            ),
            CommandParam("url", kind="option", description="Webhook URL"),
            CommandParam(
                "reason",
                kind="option",
                description="Denial message for block_if_matches",
            ),
            CommandParam(
                "matcher",
                kind="option",
                label="A|B",
                description="Tool filter for tool events",
            ),
            CommandParam(
                "command", kind="option", description="Shell command for run_command"
            ),
            CommandParam(
                "timeout",
                kind="option",
                label="N",
                description="Timeout in seconds",
            ),
            CommandParam(
                "workflow", kind="option", description="Workflow id for run_workflow"
            ),
            CommandParam(
                "workflow_params",
                kind="option",
                label="JSON",
                description="Workflow params as a quoted JSON object",
            ),
            CommandParam(
                "on_fault",
                kind="option",
                choices=("allow", "deny"),
                description="What a faulting run_workflow decides",
            ),
            CommandParam(
                "scope",
                kind="option",
                choices=("thread", "global"),
                default="thread",
                description="Bind to this thread (default) or all threads",
            ),
        )
        + _HOOK_CONDITION_PARAMS
        + (
            CommandParam(
                "once",
                kind="flag",
                type="bool",
                description="Fire at most once per thread",
            ),
            CommandParam(
                "single_use",
                kind="flag",
                type="bool",
                description="Delete the hook after it fires",
            ),
            CommandParam(
                "disabled",
                kind="flag",
                type="bool",
                description="Create it disabled",
            ),
        ),
    )
    service.register(
        "hook edit",
        description="Edit a hook (key=value scalars and/or --cond/--set)",
        category="Automation",
        aliases=("hook_edit",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
        params=(
            _HOOK_ID_PARAM,
            CommandParam(
                "fields",
                repeatable=True,
                label="key=value",
                description="Scalar edits, e.g. name=... enabled=false once=true",
            ),
        )
        + _HOOK_CONDITION_PARAMS,
    )
    service.register(
        "hook enable",
        description="Enable a hook",
        category="Automation",
        aliases=("hook_enable",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
        params=(_HOOK_ID_PARAM,),
    )
    service.register(
        "hook disable",
        description="Disable a hook",
        category="Automation",
        aliases=("hook_disable",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
        params=(_HOOK_ID_PARAM,),
    )
    service.register(
        "hook delete",
        description="Delete a hook permanently",
        category="Automation",
        aliases=("hook_delete",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        danger_level="dangerous",
        agent_allowed=False,
        params=(
            _HOOK_ID_PARAM,
            # Accepted for CLI muscle memory and by the Discord cog; it no
            # longer prompts (danger_level drives frontend confirmation).
            CommandParam(
                "yes",
                kind="flag",
                type="bool",
                aliases=("-y",),
                description="Skip confirmation",
            ),
        ),
    )
    service.register(
        "hook test",
        description="Dry-run render a hook against sample data (no fire)",
        category="Automation",
        aliases=("hook_test",),
        surfaces=_hook_sub_surfaces,
        params=(_HOOK_ID_PARAM,),
    )
    service.register(
        "hook log",
        description="Show recent hook executions (status, outcome, timing)",
        category="Automation",
        aliases=("hook_log",),
        surfaces=_hook_sub_surfaces,
        params=(
            CommandParam("id", description="Only this hook's executions"),
            CommandParam(
                "limit",
                kind="option",
                type="int",
                default=20,
                description="Rows to show (default 20)",
            ),
        ),
    )
    service.register(
        "hook templates",
        description="List the bundled hook-template catalog",
        category="Automation",
        aliases=("hook_templates",),
        surfaces=_hook_sub_surfaces,
        params=(),
    )
    service.register(
        "hook install",
        description="Install a bundled hook template as a real hook",
        category="Automation",
        aliases=("hook_install",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
        params=(
            CommandParam(
                "template_id",
                required=True,
                label="template-id",
                description="Template id from /hook templates",
            ),
            CommandParam(
                "scope",
                kind="option",
                choices=("thread", "global"),
                description="Override the template's default scope",
            ),
            CommandParam(
                "text", kind="option", description="Override the template's text"
            ),
            CommandParam(
                "disabled",
                kind="flag",
                type="bool",
                description="Install it disabled",
            ),
        ),
    )
    # Hook approvals: the human resolve surface for require_approval holds.
    # agent_allowed=False is load-bearing on approve/deny (the agent must not
    # approve its own held tool calls); like the other subcommands they stay
    # out of chat menus but still execute when a bot forwards them. This flag
    # only gates the direct "/hook <sub>" parse path; the plural "/hooks <sub>"
    # alias resolves to the parent handler, so _cmd_hook re-checks the actor
    # before re-dispatching these three (keep both gates).
    service.register(
        "hook approvals",
        description="List tool calls held awaiting your approval",
        category="Automation",
        aliases=("hook_approvals",),
        surfaces=_hook_sub_surfaces,
        agent_allowed=False,
        params=(),
    )
    service.register(
        "hook approve",
        description="Approve a held tool call (require_approval hook)",
        category="Automation",
        aliases=("hook_approve",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
        params=_HOOK_APPROVAL_PARAMS,
    )
    service.register(
        "hook deny",
        description="Deny a held tool call (require_approval hook)",
        category="Automation",
        aliases=("hook_deny",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
        params=_HOOK_APPROVAL_PARAMS,
    )
    service.register(
        "account",
        description="Inspect account, tokens, and linked platforms",
        category="Personal",
        usage="/account current|tokens|platforms",
        aliases=("acct",),
    )
    service.register(
        "account current",
        description="Show details for the current user",
        category="Personal",
        usage="/account current",
        aliases=("account_current", "account_me"),
    )
    service.register(
        "account tokens",
        description="List API tokens for the current user",
        category="Personal",
        usage="/account tokens",
        aliases=("account_tokens",),
    )
    service.register(
        "account tokens issue",
        description="Issue a new API token for the current user",
        category="Personal",
        usage="/account tokens issue [label]",
        aliases=("account_tokens_issue",),
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "account tokens revoke",
        description="Revoke an API token by hash prefix",
        category="Personal",
        usage="/account tokens revoke <hash-prefix>",
        aliases=("account_tokens_revoke",),
        mutates_state=True,
        danger_level="dangerous",
    )
    service.register(
        "account platforms",
        description="List chat platforms linked to the current user",
        category="Personal",
        usage="/account platforms",
        aliases=("account_platforms",),
    )
    service.register(
        "activity",
        description="Show recent activity and notifications",
        category="Personal",
        usage="/activity list|notifications",
    )
    service.register(
        "activity list",
        description="Show the most recent activity entries",
        category="Personal",
        usage="/activity list [limit] [--type TYPE] [--thread ID]",
        aliases=("activity_list", "activity_recent"),
    )
    service.register(
        "activity notifications",
        description="Show notifications and unread count",
        category="Personal",
        usage="/activity notifications",
        aliases=("activity_notifications",),
    )
    service.register(
        "artifacts",
        description="Inspect recent workspace artifacts",
        category="Personal",
        usage="/artifacts recent",
        requires_thread=True,
    )
    service.register(
        "artifacts recent",
        description="List recent workspace artifacts from thread history",
        category="Personal",
        usage="/artifacts recent [limit]",
        aliases=("artifacts_recent", "artifacts list"),
        requires_thread=True,
    )
    service.register(
        "doctor",
        description="Run server-side diagnostics (auth + model)",
        category="System",
        usage="/doctor [auth|model]",
    )
    service.register(
        "doctor auth",
        description="Show the resolved identity for the current request",
        category="System",
        usage="/doctor auth",
        aliases=("doctor_auth",),
    )
    service.register(
        "doctor model",
        description="Show LLM provider/model diagnostics",
        category="System",
        usage="/doctor model",
        aliases=("doctor_model",),
    )
    service.register(
        "memory list",
        description="List saved memories",
        category="Memory",
        aliases=("memory_list",),
        params=(),
    )
    service.register(
        "memory save",
        description="Save a memory",
        category="Memory",
        aliases=("memory_save",),
        mutates_state=True,
        danger_level="normal",
        examples=('/memory save color "deep blue"',),
        params=(
            CommandParam("key", required=True, description="Memory key"),
            CommandParam(
                "value",
                kind="rest",
                required=True,
                description="Value to store under the key",
            ),
        ),
    )
    service.register(
        "memory forget",
        description="Forget a memory",
        category="Memory",
        aliases=("memory_forget",),
        mutates_state=True,
        danger_level="normal",
        params=(CommandParam("key", required=True, description="Memory key to remove"),),
    )
    service.register(
        "memory search",
        description="Search saved memories",
        category="Memory",
        aliases=("memory_search",),
        params=(
            CommandParam(
                "query",
                kind="rest",
                required=True,
                description="Text to match against keys and values",
            ),
        ),
    )
    service.register(
        "memory limit",
        description="Show or change memory character limits",
        category="Memory",
        aliases=("memory_limit",),
        mutates_state=True,
        danger_level="normal",
        # A scope word then its value. The value stays a plain string because
        # the thread scope also accepts `inherit`, so the range check (and the
        # inherit synonyms) stay in the handler.
        params=(
            CommandParam(
                "scope",
                choices=("global", "thread"),
                description="Which limit to change (omit to show both)",
            ),
            CommandParam(
                "value",
                label="chars|inherit",
                description="Character limit, or `inherit` to drop a thread override",
            ),
        ),
    )
    service.register(
        "todos list",
        description="List TODOs",
        category="TODOs",
        usage="/todos list [active|pending|in_progress|done|all]",
        aliases=("todos_list",),
    )
    service.register(
        "todos add",
        description="Add a TODO",
        category="TODOs",
        usage="/todos add <task> [| <schedule>] [| <repeat>] [| <notes>]",
        aliases=("todos_add",),
        mutates_state=True,
        danger_level="normal",
        examples=("/todos add Check logs | 2h | daily",),
    )
    service.register(
        "todos complete",
        description="Complete a TODO",
        category="TODOs",
        usage="/todos complete <todo_id>",
        aliases=("todos_complete",),
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "todos delete",
        description="Delete a TODO",
        category="TODOs",
        usage="/todos delete <todo_id>",
        aliases=("todos_delete",),
        mutates_state=True,
        danger_level="dangerous",
    )
    service.register(
        "notepad read",
        description="Read this thread's notepad",
        category="Thread",
        aliases=("notepad_read",),
        requires_thread=True,
        params=(),
    )
    service.register(
        "notepad write",
        description="Write this thread's notepad",
        category="Thread",
        usage="/notepad write [append:|replace:]<content>",
        aliases=("notepad_write",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
    )  # params-exempt: raw-rest prefix grammar (append:/replace: parsed off `rest`)
    service.register(
        "notepad clear",
        description="Clear this thread's notepad",
        category="Thread",
        aliases=("notepad_clear",),
        requires_thread=True,
        mutates_state=True,
        danger_level="dangerous",
        params=(),
    )
    service.register(
        "compact",
        description="Compact the active chat context (add a focus instruction to steer the summary)",
        category="Thread",
        usage="/compact [focus instruction]",
        agent_allowed=False,
        requires_thread=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "prune",
        description="Compress tool returns in the active thread (no LLM)",
        category="Thread",
        usage="/prune [soft|full]",
        agent_allowed=False,
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "stop",
        description="Abort the running turn on this thread",
        category="Thread",
        usage="/stop",
        agent_allowed=False,
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "clear",
        description="Clear conversation history (preserves notepad + tool config)",
        category="Thread",
        usage="/clear",
        agent_allowed=False,
        requires_thread=True,
        mutates_state=True,
        danger_level="dangerous",
    )
    service.register(
        "restart api",
        description="Restart the API server process (admin-only)",
        category="System",
        usage="/restart api",
        agent_allowed=False,
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
    )
    service.register(
        "orchestrate",
        description=(
            "Decompose a goal into tasks and delegate execution to forked "
            "worker threads. Activates the 'orchestrate' skill kit on this thread."
        ),
        category="Orchestration",
        usage="/orchestrate <objective>",
        requires_thread=True,
        mutates_state=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "orchestrate clear",
        description="Exit orchestrate mode and evict its tools from this thread.",
        category="Orchestration",
        aliases=("orchestrate_clear",),
        requires_thread=True,
        mutates_state=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "orchestrate status",
        description="Show orchestrate mode status and active worker threads.",
        category="Orchestration",
        aliases=("orchestrate_status",),
        requires_thread=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "quick",
        description=(
            "Run a one-off query in a fresh, clean-context thread and show the "
            "answer inline here, without leaving the current thread. The scratch "
            "thread persists so you can continue it later via @<id> or "
            "/thread switch."
        ),
        category="Thread",
        usage="/quick <prompt>",
        requires_thread=True,
        mutates_state=True,
        agent_allowed=False,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "done",
        description=(
            "Arm a one-shot follow-up prompt that fires when the current "
            "turn finishes (a single-use DONE hook, removed after firing). "
            "With no turn running, the prompt is sent immediately."
        ),
        category="Automation",
        usage="/done <prompt>",
        requires_thread=True,
        mutates_state=True,
        agent_allowed=False,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "resume",
        description=(
            "Resume a turn that stopped at its iteration limit: re-drives "
            "the halted agent loop from the executed tool results, with a "
            "fresh safety window. No message is added to the conversation. "
            "Only valid right after an iteration-limit stop."
        ),
        category="Thread",
        usage="/resume",
        requires_thread=True,
        mutates_state=True,
        agent_allowed=False,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "goal",
        description=(
            "Start a supervised goal. The worker (this thread) decomposes "
            "the objective into tasks; a separate supervisor thread holds "
            "the authority to mark tasks complete after the user approves."
        ),
        category="Goals",
        usage="/goal <objective>",
        requires_thread=True,
        mutates_state=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "goal approve",
        description=(
            "Approve the proposed task list and spawn the supervisor thread."
        ),
        category="Goals",
        aliases=("goal_approve",),
        requires_thread=True,
        mutates_state=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "goal cancel",
        description="Abort a goal that is still pending approval.",
        category="Goals",
        aliases=("goal_cancel",),
        requires_thread=True,
        mutates_state=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "goal clear",
        description=(
            "Abort an active goal, deactivate the worker kit, and delete "
            "the supervisor thread."
        ),
        category="Goals",
        aliases=("goal_clear",),
        requires_thread=True,
        mutates_state=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "goal pause",
        description="Pause an active goal; the loop stops until resumed.",
        category="Goals",
        aliases=("goal_pause",),
        requires_thread=True,
        mutates_state=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "goal resume",
        description="Resume a paused goal and continue from where it stopped.",
        category="Goals",
        aliases=("goal_resume",),
        requires_thread=True,
        mutates_state=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "goal status",
        description=(
            "Show the active goal's status, task list, and circuit-breaker counters."
        ),
        category="Goals",
        aliases=("goal_status",),
        requires_thread=True,
        execution_kind="chat_stream",
        note="Handled by the chat stream endpoint.",
    )
    service.register(
        "goal edit",
        description="(reserved) Replace the proposed task list.",
        category="Goals",
        aliases=("goal_edit",),
        requires_thread=True,
        mutates_state=True,
        execution_kind="chat_stream",
        note="Reserved; not yet implemented.",
    )

