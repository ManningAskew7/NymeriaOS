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


# Every hook verb that names one hook takes the same argument: an id or a
# unique prefix of one (``_resolve_hook`` does the prefix match). One
# declaration keeps those usage strings from drifting apart.
_HOOK_ID_PARAM = CommandParam(
    "id",
    required=True,
    choices_ref="hooks",
    description="Hook id or unique id prefix",
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
# they share the write scope. The scope param trails the name, which is the one
# scope grammar (style guide rule 3; the ``--global`` flag these carried until
# backlog #131 wave B is gone, not aliased: a flag alias would have to stay
# grandfathered in ``command_naming``). Only `disable` takes the ``all`` value
# (the old depth-3 ``/skills off all``, folded in by backlog #131), so the two
# name params differ in their description and cannot be one constant.
_SKILL_SCOPE_PARAM = CommandParam(
    "scope",
    kind="scope",
    description="Write this thread's skill set (default) or the global one",
)
_SKILL_ENABLE_PARAMS = (
    CommandParam(
        "name",
        required=True,
        choices_ref="skills",
        description="Installed skill name",
    ),
    _SKILL_SCOPE_PARAM,
)
_SKILL_DISABLE_PARAMS = (
    CommandParam(
        "name",
        required=True,
        choices_ref="skills",
        description="Installed skill name, or `all` for every active skill",
    ),
    _SKILL_SCOPE_PARAM,
)

# Every ``/mcp`` verb that names one server takes the same argument and resolves
# it through the same registry lookup, so one declaration keeps those usage
# strings from drifting apart.
_MCP_SERVER_ID_PARAM = CommandParam(
    "server_id",
    required=True,
    label="server-id",
    choices_ref="mcp_servers",
    description="Configured MCP server id",
)

# ``/triggers enable``, ``disable``, and ``delete`` each name one trigger.
_TRIGGER_ID_PARAM = CommandParam(
    "trigger_id",
    required=True,
    label="trigger-id",
    choices_ref="triggers",
    description="Event trigger id",
)

# ``/tools enable`` and ``/tools disable`` take the same target and resolve it
# through the same category-then-tool lookup, so one declaration keeps their
# usage strings from drifting apart.
_TOOL_TARGET_PARAM = CommandParam(
    "name",
    required=True,
    choices_ref="tools",
    label="tool_or_category",
    description="Tool name or tool category",
)

# ``/todos list`` (and its ``/tasks`` alias) filters on status. Declared
# ``choices`` (#143 review): the status set is a closed enum, so the old
# label-only shape let a typo return "No <typo> TODOs." instead of a bind
# error, and cost Discord its dropdown when the family went generated.
_TODO_FILTER_PARAM = CommandParam(
    "filter",
    default="active",
    choices=("active", "pending", "in_progress", "done", "all"),
    description="Status filter (default: active)",
)

# ``/fast`` and ``/smart`` take the same bare toggle grammar.
_TIER_MODE_PARAM = CommandParam(
    "mode",
    label="on|off|set <model-id>",
    description="Turn the tier on or off (omit to toggle)",
)

# ``/fast set`` and ``/smart set`` store one tier value each. The model id is a
# rest param because the hand parser joined the remaining tokens.
_TIER_MODEL_PARAMS = (
    CommandParam(
        "model",
        kind="rest",
        required=True,
        label="model-id",
        choices_ref="models",
        description="Model id, or provider:model to route the tier elsewhere",
    ),
)

# ``/settings get`` names one setting and ``/settings set`` writes one (the
# ``/config`` spellings are aliases). The value is a rest param (the handler
# joined the remaining tokens), so a value with spaces survives.
_SETTING_KEY_PARAM = CommandParam(
    "key", required=True, description="Setting key"
)
_SETTING_WRITE_PARAMS = (
    _SETTING_KEY_PARAM,
    CommandParam(
        "value",
        kind="rest",
        required=True,
        # no_echo: the value may be a credential (LLM_API_KEY and friends);
        # keeps it out of error copy AND marks the command secret-bearing for
        # the command-hook redaction predicate (#134 review fix).
        no_echo=True,
        description="New value (coerced to bool, int, float, or string)",
    ),
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
    )  # params-exempt: the dispatcher special-cases /help before binding
    service.register(
        "status",
        description="Model, context, tools, and task summary",
        category="Status",
        requires_thread=True,
        params=(),
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
                choices_ref="threads",
                description="Thread id, id prefix, or title",
            ),
        ),
    )
    service.register(
        "thread create",
        description="Create a new thread and switch to it",
        category="Thread",
        # ``thread_new`` stays FIRST: a chat bot names a command after its
        # first single-token alias, so reordering here would silently rename
        # the Telegram menu entry. The old ``thread new`` path follows as a
        # whole-path compatibility alias (backlog #131).
        aliases=("thread_new", "thread n", "thread_create", "thread new"),
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
        "thread show",
        description="Show details for the active thread",
        category="Thread",
        # ``thread_info`` leads to keep the chat-bot menu name (see
        # ``thread create``); ``thread info`` is the old typed spelling.
        aliases=("thread_info", "thread_show", "thread info"),
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
        # ``/branch`` and ``/fork`` were a duplicate root registration; they
        # are whole-path aliases of this command now (backlog #131). They come
        # after ``thread_branch`` so the chat-bot menu name does not change.
        aliases=("thread_branch", "thread fork", "branch", "fork"),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
        params=(
            CommandParam(
                "from",
                kind="option",
                type="int",
                aliases=("-f",),
                description="Branch from this message index (1-based)",
            ),
            # Repeatable positional, NOT rest: the retired hand parser accepted
            # --from on either side of the title, and a rest tail would swallow
            # a trailing "--from 3" into the title silently (review-confirmed
            # bug).
            CommandParam(
                "title", repeatable=True, description="Title for the new branch"
            ),
        ),
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
    # /team read commands (backlog #100 phase 2). Read-only sugar over the
    # shared team serializer; mutations go through the team_manage tool, the
    # teams REST API, or the desktop UI.
    service.register(
        "team",
        description="List your callable-thread teams",
        category="Thread",
        aliases=("teams",),
        # Bare "/team" lists and "/team <ref>" is show shorthand, so the root
        # takes free text. The label keeps advertising the verbs (they are
        # registered paths, routed before this handler ever runs).
        params=(
            CommandParam(
                "team",
                kind="rest",
                label="list|show <team>",
                description="Team id or name (bare /team lists)",
            ),
        ),
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
        aliases=("team_show",),
        # A rest param, so a team name with spaces binds whether or not the
        # caller quoted it (the hand parser read the raw tail, which kept the
        # quotes and then failed to match).
        params=(
            CommandParam(
                "team",
                kind="rest",
                required=True,
                label="team-id-or-name",
                description="Team id or team name",
            ),
        ),
    )
    service.register(
        "context",
        description="Detailed context and tool breakdown",
        category="Status",
        requires_thread=True,
        params=(),
    )
    service.register(
        "usage",
        description="Show token usage and cost statistics",
        category="Status",
        aliases=("tokens", "cost"),
        requires_thread=True,
        params=(),
    )
    service.register(
        "usage session",
        description="Show session-wide (cumulative) token usage for this thread",
        category="Status",
        aliases=("usage_session",),
        requires_thread=True,
        params=(),
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
        "model list",
        description="List available provider models",
        category="LLM",
        # The number-pair resolution (backlog #131): /models was a separate
        # root, so `models` leads the aliases to keep it as the chat-bot menu
        # name for what is now a subcommand.
        aliases=("models", "model_list"),
        params=(),
    )
    # Tier commands (backlog #129). The `set`, `set-url`, and `clear` verbs used
    # to be tokens the parent handler dispatched on; registering them (the
    # /fallback idiom) gives each verb its own declared schema and lets
    # longest-prefix dispatch route it, leaving each root with the on/off
    # grammar it advertises.
    service.register(
        "fast",
        description="Switch this thread to the fast model tier",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        # Bare "/fast" toggles and on/off set the thread override; `set` is a
        # registered path routed before this handler. No choices: the root's
        # own usage error is the one that names the verbs, and the label keeps
        # the usage line advertising them.
        params=(_TIER_MODE_PARAM,),
    )
    service.register(
        "fast set",
        description="Set the model id stored for the fast tier",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        params=_TIER_MODEL_PARAMS,
    )
    service.register(
        "smart",
        description="Switch this thread to the smart model tier",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        params=(_TIER_MODE_PARAM,),
    )
    service.register(
        "smart set",
        description="Set the model id stored for the smart tier",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        params=_TIER_MODEL_PARAMS,
    )
    service.register(
        "background",
        description="Show or set the global background/utility model tier",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        # Bare "/background" shows; set, set-url, and clear are registered
        # children routed before this handler, and `show` is a whole-path alias
        # of the root. Strict zero-arg binding, so a root typo gets the
        # dispatcher's did-you-mean plus the valid-subcommand list.
        aliases=("background show",),
        params=(),
    )
    service.register(
        "background set",
        description="Set the model id stored for the background tier",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        params=_TIER_MODEL_PARAMS,
    )
    service.register(
        "background set-url",
        description="Set a base URL override for the background tier",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        params=(
            CommandParam(
                "base_url",
                kind="rest",
                required=True,
                label="base-url",
                description="Base URL the background tier should call",
            ),
        ),
    )
    service.register(
        "background clear",
        description="Clear the background tier (falls back to the main model)",
        category="LLM",
        mutates_state=True,
        danger_level="normal",
        params=(),
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
                # The valid values are the CONFIGURED CHAIN, not the model
                # catalog: a catalog-wide picker/autocomplete here offered
                # mostly values the handler rejects (review-caught).
                choices_ref="fallback_models",
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
    )  # params-exempt: int-or-note tail is not expressible as declared params
    service.register(
        "fallback deny",
        description="Decline a parked model swap (the turn proceeds on the original model's outcome)",
        category="LLM",
        usage="/fallback deny <id> [note]",
        surfaces=_fallback_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
    )  # params-exempt: int-or-note tail is not expressible as declared params
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
    )  # params-exempt: step-rail grammar over server-side pending state
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
        # A CLIProxy target id typed straight after /provider dispatches
        # here. Only the ids that are NOT registry providers: `claude`,
        # `kimi` and `grok` are registry aliases (anthropic, moonshotai,
        # xai) whose ungated, read-only provider card must keep winning,
        # and it already offers a CLIProxy tab for its matching target.
        # tests/test_command_naming.py ratchets that disjointness, so a
        # new catalog entry cannot silently hijack a provider. Injection
        # resolves BEFORE the gates and reads the resolved definition, so
        # these spellings inherit the admin/agent/chat-surface refusals
        # rather than widening access (#133).
        injected_aliases={
            "provider codex": "codex",
            "provider gemini-cli": "gemini-cli",
            "provider antigravity": "antigravity",
        },
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
    )  # params-exempt: step-rail grammar over server-side pending state
    service.register(
        "provider list",
        description="List LLM providers grouped by support tier",
        category="LLM",
        aliases=("provider_list",),
        # The default view is the SHORT one (CLIProxy targets, native and
        # gateway tiers, plus the active provider whatever its tier): the
        # unverified tier is 100+ registry entries nobody browses, and a
        # listing long enough to need truncating is a listing that lies.
        params=(
            CommandParam(
                "all",
                kind="flag",
                type="bool",
                description="Include every registry provider, unverified tier included",
            ),
            CommandParam(
                "tier",
                kind="option",
                choices=("cliproxy", "native", "gateway", "unverified"),
                description="Show one tier only",
            ),
        ),
        examples=("/provider list", "/provider list --tier cliproxy", "/provider list --all"),
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
    # /settings is the canonical settings family (backlog #131): the near-dup
    # /config family folded into it, so every `config` spelling below is a
    # whole-path alias. The bare root IS the show view (rule 2), which retires
    # `settings show` as a command in favour of an alias of the root.
    service.register(
        "settings",
        description="Show server settings",
        category="Settings",
        # get/set are registered children routed before this handler, so the
        # root binds strictly with zero arguments and a typo gets the
        # dispatcher's did-you-mean plus the valid-subcommand list.
        aliases=(
            "settings_show",
            "settings show",
            "settings view",
            "config",
            "config show",
            "config_show",
        ),
        params=(),
    )
    service.register(
        "settings get",
        description="Show one server setting",
        category="Settings",
        # A chat bot names a command after its FIRST single-token alias, so
        # `settings_get` leads: the folded-in `config_get` must not take over
        # the menu entry.
        aliases=("settings_get", "config_get", "config get"),
        params=(_SETTING_KEY_PARAM,),
    )
    service.register(
        "settings set",
        description="Change a server setting",
        category="Settings",
        aliases=("settings_set", "config_set", "config set"),
        # The fold takes the STRICTER of the two gates it merges (`config set`
        # was admin-only at dispatch, `settings set` was not). An alias may
        # never widen access, and a non-admin was refused at the update
        # surface either way, so this changes when the refusal lands, not who
        # can write a setting.
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
        note=(
            "Requires an admin user, enforced here and again at the update "
            "surface (CommandBackendClient.update_settings and PATCH /settings)."
        ),
        params=_SETTING_WRITE_PARAMS,
    )
    service.register(
        "env",
        description="Show environment variables",
        category="Settings",
        # Bare "/env" is the show view (rule 2 of the style guide). It mirrors
        # `env show`'s admin gate: the root must not be a way around it.
        requires_admin=True,
        params=(),
    )
    service.register(
        "env show",
        description="Show environment variables",
        category="Settings",
        aliases=("env_show",),
        requires_admin=True,
        params=(),
    )
    service.register(
        "env get",
        description="Show one unmasked environment variable",
        category="Settings",
        aliases=("env_get",),
        requires_admin=True,
        params=(
            CommandParam(
                "key", required=True, description="Environment variable name"
            ),
        ),
    )
    service.register(
        "env set",
        description="Change an environment variable",
        category="Settings",
        aliases=("env_set",),
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
        params=(
            CommandParam(
                "key", required=True, description="Environment variable name"
            ),
            CommandParam(
                "value",
                kind="rest",
                required=True,
                # no_echo: env values are frequently API keys; see
                # _SETTING_WRITE_PARAMS for the redaction rationale.
                no_echo=True,
                description="New value (coerced to bool, int, float, or string)",
            ),
        ),
    )

    # -- User-defined command aliases (backlog #133) ------------------------
    # Per-user spellings expanded at dispatch (`/gpt5` -> `model` plus the
    # model id). The store and its authoring-stamp control:
    # core/user_aliases.py. Agent authoring is dev-sanctioned (2026-08-03);
    # expansion resolves to canonical paths, so gates always read the real
    # command and an alias can never widen access.
    service.register(
        "alias",
        description="List your personal command aliases (family overview)",
        category="Settings",
        # The plural registers on the ROOT (whose handler IS the listing),
        # not on `alias list`: same behavior, one fewer indirection. The
        # #133 plan said the latter; recorded deviation.
        aliases=("aliases",),
        params=(),
    )
    service.register(
        "alias create",
        description="Create a personal alias that expands to a full command, values included",
        category="Settings",
        aliases=("alias_create",),
        mutates_state=True,
        danger_level="normal",
        examples=("/alias create gpt5 model openai/gpt-5.5",),
        params=(
            CommandParam(
                "name",
                required=True,
                description="The new spelling, one word (e.g. gpt5)",
            ),
            CommandParam(
                "expansion",
                kind="rest",
                required=True,
                description="The command it stands for, e.g. model openai/gpt-5.5",
            ),
        ),
    )
    service.register(
        "alias delete",
        description="Delete one of your command aliases",
        category="Settings",
        aliases=("alias_delete",),
        mutates_state=True,
        params=(
            CommandParam("name", required=True, description="Alias name"),
        ),
    )
    service.register(
        "alias list",
        description="List your command aliases with author and health flags",
        category="Settings",
        aliases=("alias_list",),
        params=(),
    )
    service.register(
        "tools",
        description="Show the tools enabled on this thread",
        category="Tools",
        # Bare "/tools" is the enabled readout, the same view as bare
        # "/tools list" (rule 2 of the style guide). Every verb is a
        # registered child routed before this handler, so the root binds
        # strictly with zero arguments and a typo gets the dispatcher's
        # did-you-mean plus the valid-subcommand list.
        params=(),
    )
    service.register(
        "tools list",
        description="List tools: enabled (default), optional, core, or one category",
        category="Tools",
        # The four listing variants (`enabled`, `optional`, `core`,
        # `category <name>`) folded into this one filter (backlog #131).
        # `tools_list` leads so it, not a folded-in spelling, names the
        # chat-bot menu entry. `tools category` bridges exactly (its tail
        # becomes the filter) and `tools enabled` bridges because `enabled`
        # is the default, so both are plain aliases. `tools core|optional`
        # (and their flat twins) CANNOT be plain aliases (they would render
        # the enabled view while claiming core/optional; the #131 review
        # caught exactly that) and were dropped until #133 built value
        # injection: they now expand to the path PLUS their filter token,
        # restoring the old views truthfully.
        aliases=(
            "tools_list",
            "tools_enabled",
            "tools_category",
            "tools enabled",
            "tools category",
        ),
        injected_aliases={
            "tools_core": "core",
            "tools core": "core",
            "tools_optional": "optional",
            "tools optional": "optional",
        },
        # requires_thread stays off: the `core` filter reads the global
        # catalog and worked without a thread before the fold. The three
        # thread-scoped filters raise the same missing-thread error from
        # inside the handler.
        # No choices: the category half of the value space is live data from
        # the tools API, and the handler already names every valid category
        # when one misses.
        params=(
            CommandParam(
                "filter",
                default="enabled",
                label="enabled|optional|core|<category>",
                description="Which tools to list (default: enabled)",
            ),
        ),
    )
    service.register(
        "tools enable",
        description="Enable a tool or category on this thread",
        category="Tools",
        aliases=("tools_enable",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        examples=("/tools enable web_search", "/tools enable productivity"),
        params=(_TOOL_TARGET_PARAM,),
    )
    service.register(
        "tools disable",
        description="Disable a tool or category on this thread",
        category="Tools",
        aliases=("tools_disable",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        params=(_TOOL_TARGET_PARAM,),
    )
    service.register(
        "sequential-tools",
        description="Show or set sequential (ordered, one-at-a-time) tool execution",
        category="Tools",
        mutates_state=True,
        danger_level="normal",
        # A mode word then the trailing scope token. `global` used to be a MODE
        # VALUE consuming a second positional (`/sequential-tools global on`),
        # which is the scope-as-a-mode-value shape the canon retired in backlog
        # #131 wave B; the old two-positional form is gone because reinstating
        # it would reinstate exactly that shape. The accepted mode set is
        # closed, so it is declared: `default` is an undocumented synonym of
        # `inherit`, kept in choices (the binder enforces what the handler
        # really takes) but out of the label, which stays the advertised form.
        params=(
            CommandParam(
                "mode",
                choices=("on", "off", "inherit", "default"),
                label="on|off|inherit",
                description="Turn it on/off, or `inherit` to drop the override",
            ),
            CommandParam(
                "scope",
                kind="scope",
                description="Write the global default or this thread's override",
            ),
        ),
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
        examples=("/skills show summarize", "/skills enable summarize global"),
        # Bare "/skills" lists. Every verb is a registered child routed before
        # this handler by longest-prefix dispatch, so the root binds strictly
        # with zero arguments and a typo gets the dispatcher's did-you-mean
        # plus the valid-subcommand list.
        params=(),
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
        description="Show a skill's metadata and markdown body without activating it",
        category="Skills",
        # `skills inspect` folded in here (backlog #131): one show-one verb,
        # one handler, and the union of the two outputs. `skills_show` leads
        # so the chat-bot menu entry keeps its name.
        aliases=("skills_show", "skills_inspect", "skills inspect"),
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
        # The name is free text for the same reason: it names a skill in a
        # remote catalog, so the "skills" ref (INSTALLED skills, what every
        # other /skills verb takes) would offer exactly the wrong set here.
        params=(
            CommandParam(
                "name",
                required=True,
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
        params=_SKILL_ENABLE_PARAMS,
    )
    service.register(
        "skills disable",
        description="Disable a skill, or `all` to deactivate every active one",
        category="Skills",
        # The depth-3 `skills off all` folded in here (backlog #131): `off`
        # is a whole-path alias of this command, so "/skills off all" and
        # "/skills off <name>" both parse, and `all` is a value the handler
        # takes. `skills_disable` leads so the chat-bot menu keeps its name;
        # the flat `skills_off_all` spelling is dropped with the depth-3 path.
        aliases=("skills_disable", "skills off"),
        mutates_state=True,
        params=_SKILL_DISABLE_PARAMS,
    )
    service.register(
        "mcp",
        description="MCP server management commands",
        category="MCP",
        requires_admin=True,
        # Bare "/mcp" lists; every verb is a registered child routed before this
        # handler, so the root binds strictly with zero arguments and a typo
        # gets the dispatcher's did-you-mean plus the valid-subcommand list.
        params=(),
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
                choices_ref="mcp_servers",
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
        "mcp delete",
        description="Remove an MCP server and its tools",
        category="MCP",
        # `mcp_remove` leads so the chat-bot menu entry keeps its name; the
        # old `mcp remove` path is a whole-path alias (backlog #131).
        aliases=("mcp_remove", "mcp_rm", "mcp_delete", "mcp remove", "mcp rm"),
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
        # Bare "/triggers" lists; every verb is a registered child routed before
        # this handler, so the root binds strictly with zero arguments and a
        # typo gets the dispatcher's did-you-mean plus the valid-subcommand
        # list.
        params=(),
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
                choices_ref="triggers",
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
        # Bare "/hook" lists. Every verb is a registered child routed before
        # this handler by longest-prefix dispatch (the "detail" synonym of show
        # is a whole-path alias on `hook show`), so the root binds strictly with
        # zero arguments and a typo gets the dispatcher's did-you-mean plus the
        # valid-subcommand list.
        params=(),
    )
    service.register(
        "hook list",
        description="List lifecycle hooks",
        category="Automation",
        aliases=("hook_list",),
        surfaces=_hook_sub_surfaces,
        # The trailing scope token is the FILTER axis here, not a write scope,
        # so omitting it lists every hook rather than defaulting to the thread
        # (backlog #131 wave B; a list command's default is unfiltered). It
        # composes with ``--thread``, which selects WHICH thread: the two are
        # different questions and the binder's option guard keeps
        # ``--thread global`` binding the option rather than popping a scope.
        params=(
            CommandParam(
                "thread",
                kind="option",
                label="id|current",
                description="Show hooks bound to this thread (`current` for the active one)",
            ),
            CommandParam(
                "enabled_only",
                kind="flag",
                type="bool",
                description="Hide disabled hooks",
            ),
            CommandParam(
                "scope",
                kind="scope",
                description="Show only global hooks, or this thread's view (its hooks plus the global ones that also fire here)",
            ),
        ),
    )
    service.register(
        "hook show",
        description="Show one hook's full configuration",
        category="Automation",
        aliases=("hook_show", "hook_detail", "hook detail"),
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
            '/hook create greet --event prompt_submit --action inject_context --text "Be brief."',
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
                description=(
                    "Name filter: tool names on tool events, command paths "
                    "on command-submit (trailing * matches a family)"
                ),
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
        "hook history",
        description="Show recent hook executions (status, outcome, timing)",
        category="Automation",
        # Aligned with `triggers history` (backlog #131); `log` stays reserved
        # for raw log output (`mcp logs`). `hook_log` leads so the chat-bot
        # menu entry keeps its name.
        aliases=("hook_log", "hook_history", "hook log"),
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
        aliases=("acct",),
        # Bare "/account" shows the current user; the verbs are registered
        # children routed before this handler ever runs, so the root binds
        # strictly with zero arguments and a typo gets the dispatcher's
        # did-you-mean plus the valid-subcommand list.
        params=(),
    )
    service.register(
        "account show",
        description="Show details for the current user",
        category="Personal",
        # `account_current` leads so the chat-bot menu entry keeps its name;
        # the old `account current` path and the `me` synonym follow as
        # whole-path aliases (backlog #131).
        aliases=(
            "account_current",
            "account_me",
            "account_show",
            "account current",
            "account me",
        ),
        params=(),
    )
    service.register(
        "account tokens",
        description="List API tokens for the current user",
        category="Personal",
        aliases=("account_tokens",),
        params=(),
    )
    service.register(
        "account tokens issue",
        description="Issue a new API token for the current user",
        category="Personal",
        aliases=("account_tokens_issue",),
        mutates_state=True,
        danger_level="normal",
        params=(
            CommandParam(
                "label",
                kind="rest",
                description="Label recorded against the new token",
            ),
        ),
    )
    service.register(
        "account tokens revoke",
        description="Revoke an API token by hash prefix",
        category="Personal",
        aliases=("account_tokens_revoke",),
        mutates_state=True,
        danger_level="dangerous",
        params=(
            CommandParam(
                "prefix",
                required=True,
                label="hash-prefix",
                description="Token hash prefix shown by /account tokens",
            ),
        ),
    )
    service.register(
        "account platforms",
        description="List chat platforms linked to the current user",
        category="Personal",
        aliases=("account_platforms",),
        params=(),
    )
    service.register(
        "activity",
        description="Show recent activity and notifications",
        category="Personal",
        # Bare "/activity" lists; list and notifications are registered children
        # routed before this handler (`recent` is a whole-path alias of
        # `activity list`), so the root binds strictly with zero arguments and a
        # typo gets the dispatcher's did-you-mean plus the valid-subcommand
        # list.
        params=(),
    )
    service.register(
        "activity list",
        description="Show the most recent activity entries",
        category="Personal",
        aliases=("activity_list", "activity_recent", "activity recent"),
        # --type is validated against the ActivityType enum in the handler
        # (live value set), and --thread accepts `current`/`.` for this thread.
        params=(
            CommandParam(
                "limit",
                type="int",
                default=20,
                description="How many entries to show",
            ),
            CommandParam(
                "type",
                kind="option",
                description="Activity type to filter on",
            ),
            CommandParam(
                "thread",
                kind="option",
                label="ID",
                description="Thread id, or `current` for this thread",
            ),
        ),
    )
    service.register(
        "activity notifications",
        description="Show notifications and unread count",
        category="Personal",
        aliases=("activity_notifications",),
        params=(),
    )
    service.register(
        "artifacts",
        description="Inspect recent workspace artifacts",
        category="Personal",
        requires_thread=True,
        params=(),
    )
    service.register(
        "artifacts list",
        description="List recent workspace artifacts from thread history",
        category="Personal",
        # `list` was the alias and `recent` the path; backlog #131 flips them.
        # `artifacts_recent` leads so the chat-bot menu entry keeps its name.
        aliases=("artifacts_recent", "artifacts_list", "artifacts recent"),
        requires_thread=True,
        params=(
            CommandParam(
                "limit",
                type="int",
                default=10,
                description="How many artifacts to show",
            ),
        ),
    )
    service.register(
        "doctor",
        description="Run server-side diagnostics (auth + model)",
        category="System",
        # Bare "/doctor" runs both sections; each section is a registered child
        # routed before this handler, so the root binds strictly with zero
        # arguments and a typo gets the dispatcher's did-you-mean plus the
        # valid-subcommand list.
        params=(),
    )
    service.register(
        "doctor auth",
        description="Show the resolved identity for the current request",
        category="System",
        aliases=("doctor_auth",),
        params=(),
    )
    service.register(
        "doctor model",
        description="Show LLM provider/model diagnostics",
        category="System",
        aliases=("doctor_model",),
        params=(),
    )
    service.register(
        "memory",
        description="List saved memories",
        category="Memory",
        # Bare "/memory" lists (rule 2 of the style guide). Every verb is a
        # registered child routed before this handler, so the root binds
        # strictly with zero arguments and a typo gets the dispatcher's
        # did-you-mean plus the valid-subcommand list.
        params=(),
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
        "memory delete",
        description="Forget a memory",
        category="Memory",
        # `memory_forget` leads so the chat-bot menu entry keeps its name; the
        # old `memory forget` path is a whole-path alias (backlog #131).
        aliases=("memory_forget", "memory_delete", "memory forget"),
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
        # The value then the trailing scope token (backlog #131 wave B; the
        # scope used to be a leading positional with its own choice set). The
        # scope pops from the END before positionals are assigned, so it can
        # never steal the value. The value stays a plain string because the
        # thread scope also accepts `inherit`, so the range check (and the
        # inherit synonyms) stay in the handler.
        params=(
            CommandParam(
                "value",
                label="chars|inherit",
                description="Character limit, or `inherit` to drop a thread override",
            ),
            CommandParam(
                "scope",
                kind="scope",
                description="Which limit to change (omit both to show them)",
            ),
        ),
    )
    service.register(
        "todos",
        description="List TODOs",
        category="TODOs",
        # Bare "/todos" lists (rule 2 of the style guide). Every verb is a
        # registered child routed before this handler, so the root binds
        # strictly with zero arguments and a typo gets the dispatcher's
        # did-you-mean plus the valid-subcommand list. The singular family
        # (`/todo ...`) is whole-path aliases per verb (#143 folded the
        # CLI-local family into this one), per rule 2's number aliasing.
        aliases=("todo",),
        params=(),
    )
    service.register(
        "todos list",
        description="List TODOs",
        category="TODOs",
        # `/tasks` was a near-duplicate root with its own implementation;
        # backlog #131 folds it in as a whole-path alias of this command.
        aliases=("todos_list", "tasks", "todo list"),
        params=(
            _TODO_FILTER_PARAM,
            CommandParam(
                "thread",
                kind="option",
                label="current|<id>",
                description="Only TODOs on this thread (`current` for the active one)",
            ),
        ),
    )
    service.register(
        "todos add",
        description="Add a TODO",
        category="TODOs",
        aliases=("todos_add", "todo add"),
        mutates_state=True,
        danger_level="normal",
        params=(
            # Repeatable positional, NOT rest: options stay bindable on
            # either side of the task text (the /thread branch precedent),
            # and the legacy pipe grammar re-collects intact from the
            # joined tokens (the handler still splits on `|`).
            CommandParam(
                "task",
                repeatable=True,
                required=True,
                description="Task text",
            ),
            CommandParam(
                "schedule",
                kind="option",
                aliases=("--when",),
                description="When to fire (45s/2h/1d or absolute; `none` for no schedule; default 1d)",
            ),
            CommandParam(
                "notes",
                kind="option",
                description="Notes attached to the TODO",
            ),
            CommandParam(
                "repeat",
                kind="option",
                aliases=("--recurrence",),
                description="Recurrence interval (daily, 2h, weekly, ...)",
            ),
            CommandParam(
                "thread",
                kind="option",
                label="current|<id>",
                description="Thread to attach to (default: current)",
            ),
        ),
        examples=(
            "/todos add Check logs --schedule 2h --repeat daily",
            "/todos add Check logs | 2h | daily",
        ),
    )
    service.register(
        "todos edit",
        description="Edit a TODO",
        category="TODOs",
        aliases=("todos_edit", "todo edit"),
        mutates_state=True,
        danger_level="normal",
        params=(
            CommandParam(
                "todo_id",
                required=True,
                choices_ref="todos",
                description="TODO id or unique id prefix",
            ),
            CommandParam(
                "task",
                repeatable=True,
                description="New task text",
            ),
            CommandParam(
                "status",
                kind="option",
                choices=("pending", "in_progress", "done"),
                description="New status",
            ),
            CommandParam(
                "notes",
                kind="option",
                description="Replace the notes",
            ),
            CommandParam(
                "schedule",
                kind="option",
                aliases=("--when",),
                description="New fire time",
            ),
            CommandParam(
                "clear_schedule",
                kind="flag",
                type="bool",
                description="Remove the schedule",
            ),
            CommandParam(
                "repeat",
                kind="option",
                aliases=("--recurrence",),
                description="New recurrence interval",
            ),
            CommandParam(
                "clear_repeat",
                kind="flag",
                type="bool",
                aliases=("--clear-recurrence",),
                description="Remove the recurrence",
            ),
            CommandParam(
                "thread",
                kind="option",
                label="current|<id>",
                description="Rebind to a thread",
            ),
        ),
        examples=("/todos edit 1a2b3c Water the plants --schedule 2h",),
    )
    service.register(
        "todos schedule",
        description="Set or clear a TODO's fire time",
        category="TODOs",
        aliases=("todos_schedule", "todo schedule"),
        mutates_state=True,
        danger_level="normal",
        params=(
            CommandParam(
                "todo_id",
                required=True,
                choices_ref="todos",
                description="TODO id or unique id prefix",
            ),
            # Repeatable: absolute times span tokens ("2026-08-05 09:00").
            CommandParam(
                "when",
                repeatable=True,
                required=True,
                label="when|clear",
                description="New fire time, or `clear` to remove it",
            ),
        ),
    )
    service.register(
        "todos repeat",
        description="Set or clear a TODO's recurrence",
        category="TODOs",
        aliases=("todos_repeat", "todos recurrence", "todo repeat", "todo recurrence"),
        mutates_state=True,
        danger_level="normal",
        params=(
            CommandParam(
                "todo_id",
                required=True,
                choices_ref="todos",
                description="TODO id or unique id prefix",
            ),
            CommandParam(
                "interval",
                required=True,
                label="interval|clear",
                description="Recurrence interval (daily, 2h, weekly, ...), or `clear`",
            ),
        ),
    )
    service.register(
        "todos complete",
        description="Complete a TODO",
        category="TODOs",
        aliases=("todos_complete", "todo done", "todo complete"),
        mutates_state=True,
        danger_level="normal",
        params=(
            CommandParam(
                "todo_id",
                required=True,
                choices_ref="todos",
                description="TODO id or unique id prefix",
            ),
        ),
    )
    service.register(
        "todos delete",
        description="Delete a TODO",
        category="TODOs",
        aliases=("todos_delete", "todo delete", "todo remove", "todo rm"),
        mutates_state=True,
        danger_level="dangerous",
        params=(
            CommandParam(
                "todo_id",
                required=True,
                choices_ref="todos",
                description="TODO id or unique id prefix",
            ),
            # The retired CLI-local family took --yes for its own confirm
            # prompt; accepted as a no-op so old muscle memory does not
            # usage-error. Generic danger confirmation is backlog #147.
            CommandParam(
                "yes",
                kind="flag",
                type="bool",
                description="Accepted for compatibility; deletion does not prompt",
            ),
        ),
    )
    service.register(
        "notepad",
        description="Read this thread's notepad",
        category="Thread",
        # Bare "/notepad" reads (rule 2 of the style guide). Every verb is a
        # registered child routed before this handler, so the root binds
        # strictly with zero arguments and a typo gets the dispatcher's
        # did-you-mean plus the valid-subcommand list.
        requires_thread=True,
        params=(),
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
        agent_allowed=False,
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        params=(
            CommandParam(
                "mode",
                choices=("soft", "full"),
                default="full",
                description=(
                    "soft truncates each tool result to 500 chars; full drops "
                    "it to a placeholder"
                ),
            ),
        ),
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
    )  # params-exempt: act-now, trailing words must never block the stop
    service.register(
        "clear",
        description="Clear conversation history (preserves notepad + tool config)",
        category="Thread",
        usage="/clear",
        agent_allowed=False,
        requires_thread=True,
        mutates_state=True,
        danger_level="dangerous",
    )  # params-exempt: act-now, trailing words must never block the clear
    service.register(
        "restart api",
        description="Restart the API server process (admin-only)",
        category="System",
        usage="/restart api",
        # Flat-alias convention (provider_set, orchestrate_clear, ...): its
        # absence left /restart_api answered by a did-you-mean pointing at
        # bare /restart, which bot-local handlers intercept on bot surfaces
        # (restarting the BOT, not the API) and which elsewhere only yields
        # the subcommand listing (2026-08-10 outage, bug 3).
        aliases=("restart_api",),
        agent_allowed=False,
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
    )  # params-exempt: act-now, trailing words must never block the restart
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
