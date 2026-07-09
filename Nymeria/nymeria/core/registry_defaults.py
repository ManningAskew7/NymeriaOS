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

if TYPE_CHECKING:
    from .command_service import CommandService


def register_default_commands(service: "CommandService") -> None:
    """Register every built-in slash command on ``service`` in catalog order.

    Order matters only for diff stability and the F7 executor-binding guard
    test's parametrize ids; the registry sorts on read (``list_commands``) and
    ``validate_registry`` is order-independent.
    """
    service.register("help", description="Show available commands", category="General", aliases=("h",))
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
        usage="/thread list",
        aliases=("thread_list",),
    )
    service.register(
        "thread switch",
        description="Switch the active thread",
        category="Thread",
        usage="/thread switch <id-or-title>",
        aliases=("thread_switch", "thread s"),
        agent_allowed=False,
    )
    service.register(
        "thread new",
        description="Create a new thread and switch to it",
        category="Thread",
        usage="/thread new [title]",
        aliases=("thread_new", "thread n"),
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
    )
    service.register(
        "thread delete",
        description="Delete a thread permanently",
        category="Thread",
        usage="/thread delete <id> [--yes]",
        aliases=("thread_delete", "thread del", "thread rm"),
        mutates_state=True,
        danger_level="dangerous",
        agent_allowed=False,
    )
    service.register(
        "thread info",
        description="Show details for the active thread",
        category="Thread",
        usage="/thread info",
        aliases=("thread_info",),
        requires_thread=True,
    )
    service.register(
        "thread rename",
        description="Rename the active thread",
        category="Thread",
        usage="/thread rename <title>",
        aliases=("thread_rename",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
    )
    service.register(
        "thread pin",
        description="Pin or unpin a thread",
        category="Thread",
        usage="/thread pin [id] [on|off|toggle]",
        aliases=("thread_pin",),
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
    )
    service.register(
        "thread config",
        description="Show the active thread's configuration",
        category="Thread",
        usage="/thread config",
        aliases=("thread_config",),
        requires_thread=True,
    )
    service.register(
        "thread branch",
        description="Branch the active thread into a new thread",
        category="Thread",
        usage="/thread branch [--from N] [title]",
        aliases=("thread_branch", "thread fork"),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
    )
    service.register(
        "thread compact",
        description="Compact the active thread's context",
        category="Thread",
        usage="/thread compact [--yes]",
        aliases=("thread_compact",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
    )
    service.register(
        "branch",
        description="Branch the active thread into a new thread",
        category="Thread",
        usage="/branch [--from N] [title]",
        aliases=("fork",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
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
        usage="/model [name] [global|thread]",
        mutates_state=True,
        danger_level="normal",
    )
    service.register("models", description="List available provider models", category="LLM")
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
        description="Manage the model fallback chain",
        category="LLM",
        usage="/fallback [list|add|remove|clear|set]",
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "think",
        description="Show or change thinking mode (thread-scoped when a thread is active)",
        category="LLM",
        usage="/think [off|on|low|medium|high|xhigh|max] [global|thread]",
        aliases=("reasoning", "thinking"),
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "provider",
        description="Show the active LLM provider and credential status",
        category="LLM",
        usage="/provider [list|set|switch|test|reasoning-passback]",
    )
    service.register(
        "provider list",
        description="List LLM providers grouped by support tier",
        category="LLM",
        aliases=("provider_list",),
    )
    service.register(
        "provider set",
        description="Apply provider credentials to backend settings",
        category="LLM",
        usage="/provider set <provider> <key=value> [key=value...]",
        aliases=("provider_set",),
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
    )
    service.register(
        "provider test",
        description="Test provider connectivity without saving anything",
        category="LLM",
        usage="/provider test [provider]",
        aliases=("provider_test",),
        requires_admin=True,
    )
    service.register(
        "provider switch",
        description="Switch the active LLM provider",
        category="LLM",
        usage="/provider switch <provider>",
        aliases=("provider_switch",),
        requires_admin=True,
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "provider reasoning-passback",
        description="Show whether prior-turn reasoning is replayed to the model",
        category="LLM",
        usage="/provider reasoning-passback",
        aliases=("provider_reasoning_passback", "provider passback"),
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
        usage="/skills",
        requires_thread=True,
    )
    service.register(
        "skills list",
        description="List skills visible on this thread",
        category="Skills",
        usage="/skills list",
        aliases=("skills_list",),
        requires_thread=True,
    )
    service.register(
        "skills show",
        description="Show a skill's markdown body without activating it",
        category="Skills",
        usage="/skills show <name>",
        aliases=("skills_show",),
    )
    service.register(
        "skills off all",
        description="Deactivate every visible skill active on this thread",
        category="Skills",
        usage="/skills off all",
        aliases=("skills_off_all",),
        requires_thread=True,
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "skills search",
        description="Search a skills marketplace for installable skills",
        category="Skills",
        usage="/skills search [query] [--source <source>]",
        aliases=("skills_search",),
    )
    service.register(
        "skills install",
        description="Install a skill from a marketplace",
        category="Skills",
        usage="/skills install <name> [--source <source>] [--scope user|global]",
        aliases=("skills_install",),
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "skills enable",
        description="Enable a skill on this thread (default) or globally",
        category="Skills",
        usage="/skills enable [--global] <name>",
        aliases=("skills_enable",),
        mutates_state=True,
    )
    service.register(
        "skills disable",
        description="Disable a skill on this thread (default) or globally",
        category="Skills",
        usage="/skills disable [--global] <name>",
        aliases=("skills_disable",),
        mutates_state=True,
    )
    service.register(
        "skills inspect",
        description="Show full skill details (metadata, scope, tools, references)",
        category="Skills",
        usage="/skills inspect <name>",
        aliases=("skills_inspect",),
    )
    service.register(
        "mcp",
        description="MCP server management commands",
        category="MCP",
        usage="/mcp list|status|logs|discover|test|remove|retry [...]",
        requires_admin=True,
    )
    service.register(
        "mcp list",
        description="List configured MCP servers",
        category="MCP",
        usage="/mcp list",
        aliases=("mcp_list",),
        requires_admin=True,
    )
    service.register(
        "mcp status",
        description="Show MCP server status, with errors if any",
        category="MCP",
        usage="/mcp status [server-id]",
        aliases=("mcp_status",),
        requires_admin=True,
    )
    service.register(
        "mcp logs",
        description="Show install logs for an MCP server",
        category="MCP",
        usage="/mcp logs <server-id> [limit]",
        aliases=("mcp_logs",),
        requires_admin=True,
    )
    service.register(
        "mcp discover",
        description="Force tool rediscovery for an MCP server",
        category="MCP",
        usage="/mcp discover <server-id>",
        aliases=("mcp_discover",),
        requires_admin=True,
        mutates_state=True,
    )
    service.register(
        "mcp test",
        description="Test connectivity to an MCP server",
        category="MCP",
        usage="/mcp test <server-id>",
        aliases=("mcp_test",),
        requires_admin=True,
    )
    service.register(
        "mcp remove",
        description="Remove an MCP server and its tools",
        category="MCP",
        usage="/mcp remove <server-id>",
        aliases=("mcp_remove", "mcp_rm", "mcp_delete"),
        requires_admin=True,
        mutates_state=True,
        danger_level="dangerous",
    )
    service.register(
        "mcp retry",
        description="Retry setup for a draft or failed MCP server",
        category="MCP",
        usage="/mcp retry <server-id>",
        aliases=("mcp_retry",),
        requires_admin=True,
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "triggers",
        description="Event-trigger automation commands",
        category="Automation",
        usage="/triggers list|enable|disable|delete|history [...]",
    )
    service.register(
        "triggers list",
        description="List configured event triggers",
        category="Automation",
        usage="/triggers list [--enabled-only] [--thread <id>]",
        aliases=("triggers_list",),
    )
    service.register(
        "triggers enable",
        description="Enable an event trigger",
        category="Automation",
        usage="/triggers enable <trigger-id>",
        aliases=("triggers_enable",),
        mutates_state=True,
    )
    service.register(
        "triggers disable",
        description="Disable an event trigger",
        category="Automation",
        usage="/triggers disable <trigger-id>",
        aliases=("triggers_disable",),
        mutates_state=True,
    )
    service.register(
        "triggers delete",
        description="Delete an event trigger permanently",
        category="Automation",
        usage="/triggers delete <trigger-id>",
        aliases=("triggers_delete",),
        mutates_state=True,
        danger_level="dangerous",
    )
    service.register(
        "triggers history",
        description="Show recent trigger execution history",
        category="Automation",
        usage="/triggers history [trigger-id] [--limit N]",
        aliases=("triggers_history",),
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
        usage=(
            "/hook list|create|show|edit|enable|disable|delete|test|log"
            "|approvals|approve|deny [...]"
        ),
        aliases=("hooks",),
    )
    service.register(
        "hook list",
        description="List lifecycle hooks",
        category="Automation",
        usage="/hook list [--thread <id>|current] [--global] [--enabled-only]",
        aliases=("hook_list",),
        surfaces=_hook_sub_surfaces,
    )
    service.register(
        "hook show",
        description="Show one hook's full configuration",
        category="Automation",
        usage="/hook show <id>",
        aliases=("hook_show", "hook_detail"),
        surfaces=_hook_sub_surfaces,
    )
    service.register(
        "hook create",
        description="Create a lifecycle hook",
        category="Automation",
        usage=(
            "/hook create <name> --event E --action A "
            '[--text ..|--url ..|--cond "f op v"..|--reason ..|--set arg=val..'
            "|--command ..|--timeout N] "
            '[--fire-cond "f op v"]... [--once] '
            "[--matcher A|B] [--scope thread|global] [--disabled]"
        ),
        aliases=("hook_create",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
    )
    service.register(
        "hook edit",
        description="Edit a hook (key=value scalars and/or --cond/--set)",
        category="Automation",
        usage=(
            '/hook edit <id> [key=value]... [--cond "f op v"]... '
            '[--fire-cond "f op v"]... [--set arg=val]... [once=true|false]'
        ),
        aliases=("hook_edit",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        danger_level="normal",
        agent_allowed=False,
    )
    service.register(
        "hook enable",
        description="Enable a hook",
        category="Automation",
        usage="/hook enable <id>",
        aliases=("hook_enable",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
    )
    service.register(
        "hook disable",
        description="Disable a hook",
        category="Automation",
        usage="/hook disable <id>",
        aliases=("hook_disable",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
    )
    service.register(
        "hook delete",
        description="Delete a hook permanently",
        category="Automation",
        usage="/hook delete <id> [--yes]",
        aliases=("hook_delete",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        danger_level="dangerous",
        agent_allowed=False,
    )
    service.register(
        "hook test",
        description="Dry-run render a hook against sample data (no fire)",
        category="Automation",
        usage="/hook test <id>",
        aliases=("hook_test",),
        surfaces=_hook_sub_surfaces,
    )
    service.register(
        "hook log",
        description="Show recent hook executions (status, outcome, timing)",
        category="Automation",
        usage="/hook log [id] [--limit N]",
        aliases=("hook_log",),
        surfaces=_hook_sub_surfaces,
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
        usage="/hook approvals",
        aliases=("hook_approvals",),
        surfaces=_hook_sub_surfaces,
        agent_allowed=False,
    )
    service.register(
        "hook approve",
        description="Approve a held tool call (require_approval hook)",
        category="Automation",
        usage="/hook approve <approval-id> [note]",
        aliases=("hook_approve",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
    )
    service.register(
        "hook deny",
        description="Deny a held tool call (require_approval hook)",
        category="Automation",
        usage="/hook deny <approval-id> [note]",
        aliases=("hook_deny",),
        surfaces=_hook_sub_surfaces,
        mutates_state=True,
        agent_allowed=False,
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
    )
    service.register(
        "memory save",
        description="Save a memory",
        category="Memory",
        usage="/memory save <key> <value>",
        aliases=("memory_save",),
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "memory forget",
        description="Forget a memory",
        category="Memory",
        usage="/memory forget <key>",
        aliases=("memory_forget",),
        mutates_state=True,
        danger_level="normal",
    )
    service.register(
        "memory search",
        description="Search saved memories",
        category="Memory",
        usage="/memory search <query>",
        aliases=("memory_search",),
    )
    service.register(
        "memory limit",
        description="Show or change memory character limits",
        category="Memory",
        usage="/memory limit [global <chars>|thread <chars>|thread inherit]",
        aliases=("memory_limit",),
        mutates_state=True,
        danger_level="normal",
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
    )
    service.register(
        "notepad clear",
        description="Clear this thread's notepad",
        category="Thread",
        aliases=("notepad_clear",),
        requires_thread=True,
        mutates_state=True,
        danger_level="dangerous",
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

