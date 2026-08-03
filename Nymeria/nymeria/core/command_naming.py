"""Naming-canon validation for the built-in command catalog (backlog #131).

The human rules live in ``docs/private/command-style-guide.md``; this module
is their machine-checkable half, called from
``CommandService.validate_registry()`` at construction. Scope is the
BUILT-IN catalog only: runtime registrations (tests, plugins, agent-authored
commands) are exempt by design, because agents are users too and the canon
governs what the project ships, not what a user builds.

Three constant families, each shrink-or-justify:

- ``SANCTIONED_LEAF_TOKENS``: every non-canon leaf token in service, each
  with the reason it is the right word. Adding a new command with a new
  leaf means either using a canon verb or recording the domain word HERE
  (and in the style guide's human copy, kept in sync by hand).
- ``_GRANDFATHERED_*``: catalog entries that predate the canon and are
  scheduled for migration in the #131 rename waves. SHRINK ONLY: the
  validator fails on a stale entry, so a rename forcibly retires its row,
  and nothing may be added without a recorded decision.
- ``AGENT_BLOCKED_UNREGISTERED``: names the agent gate pre-blocks that are
  deliberately NOT registry commands today (bot-local chat entries); the
  validator accepts these and fails on anything else unregistered.
"""

from __future__ import annotations

from typing import Any, Mapping

# The generic verb set, one meaning each (style guide "Verbs" table).
CANON_VERBS: frozenset[str] = frozenset(
    {
        "list",
        "show",
        "status",
        "create",
        "add",
        "delete",
        "remove",
        "clear",
        "enable",
        "disable",
        "set",
    }
)

# Non-canon leaf tokens in service, token -> reason. Underscore-normalized
# spellings (the registry's storage form).
SANCTIONED_LEAF_TOKENS: dict[str, str] = {
    # Consent and lifecycle flows
    "approve": "consent flow verb (hooks, fallback, goal)",
    "deny": "consent flow verb",
    "approvals": "noun sub-list: pending consent queue",
    "revert": "consent-flow undo of an applied fallback swap",
    "pause": "goal lifecycle verb",
    "resume": "goal lifecycle verb",
    "cancel": "goal lifecycle verb",
    "edit": "in-place mutation of hooks and goals",
    "complete": "todos are completed, not deleted",
    # Domain verbs where the generic word would lie
    "switch": "threads and providers are switched, not set",
    "save": "memories are saved (memory family)",
    "search": "semantic search (memory, skills)",
    "limit": "memory char-limit setting",
    "read": "notepad value-container trio read/write/clear",
    "write": "notepad value-container trio",
    "get": "settings/env key access, the universal config pairing",
    "issue": "tokens are issued",
    "revoke": "tokens are revoked",
    "branch": "thread branching",
    "compact": "context compaction",
    "rename": "thread rename",
    "pin": "thread pin toggle",
    "install": "skills, hooks: fetched then installed",
    "discover": "mcp tool discovery",
    "retry": "mcp reconnect",
    "test": "connectivity probes (mcp, hook, provider)",
    "logs": "raw log OUTPUT (mcp); distinct from history",
    "history": "past executions (hook, triggers)",
    "setup": "the chained provider setup flow",
    "set_url": "URL variant of set (renders set-url)",
    # Noun leaves: sub-family collections and named views
    "platforms": "noun sub-list: linked platforms",
    "tokens": "noun sub-family: account tokens",
    "notifications": "noun sub-list: notification routes",
    "templates": "noun sub-list: hook templates",
    "auth": "doctor section name",
    "model": "doctor section name",
    "api": "restart target name",
    "session": "usage view name",
    "cliproxy": "provider sub-family: CLIProxy OAuth chain",
    "reasoning_passback": "provider setting name (renders reasoning-passback)",
    "config": "thread config view (distinct from thread show)",
}

# Catalog ids whose LEAF predates the canon; each is renamed by a #131
# wave. The validator fails on a stale id, so the rename retires the row.
# EMPTY since wave A landed every leaf rename in the table; an entry here
# again means a new command shipped with a pre-canon leaf.
_GRANDFATHERED_LEAVES: frozenset[str] = frozenset()

# Families allowed to have subcommands but no bare root.
ROOTLESS_FAMILY_EXCEPTIONS: dict[str, str] = {
    "restart": "a bare /restart must never have a default action",
}
# EMPTY since wave A: env, memory, notepad, todos and tools gained overview
# roots and the config family folded into /settings.
_ROOTLESS_GRANDFATHERED: frozenset[str] = frozenset()

# Scope grammar: the canon is a trailing global|thread token declared as
# the `scope` param kind. These ids carry the pre-canon --global flag until
# wave B migrates them.
_SCOPE_FLAG_GRANDFATHERED: frozenset[str] = frozenset(
    {"skills.enable", "skills.disable", "hook.list"}
)
# Recorded permanent exceptions: a rest-primary command cannot declare the
# scope kind (parser rule), and skills.install's scope is a different axis.
SCOPE_OPTION_EXCEPTIONS: dict[str, str] = {
    "hook.create": "rest primary; scope kind structurally unavailable",
    "hook.install": "rest primary; scope kind structurally unavailable",
    "skills.install": "user|global install-target axis, not write scope",
}

# Agent-gate entries that are deliberately NOT registry commands: bot-local
# chat entries pre-blocked so that registering them someday cannot quietly
# hand them to the agent.
AGENT_BLOCKED_UNREGISTERED: frozenset[str] = frozenset({"ask", "start"})


def validate_command_naming(
    commands: Mapping[str, Any],
    agent_blocked: frozenset[str] | set[str],
    *,
    grandfathered: frozenset[str] = _GRANDFATHERED_LEAVES,
    rootless_grandfathered: frozenset[str] = _ROOTLESS_GRANDFATHERED,
) -> None:
    """Raise ``ValueError`` on the first canon violation in ``commands``.

    ``commands`` maps command id -> CommandDefinition (duck-typed: needs
    ``path`` and ``params``). Runs at ``CommandService`` construction, so
    every test that builds a service exercises it. The keyword sets exist
    so unit tests can exercise one rule against a synthetic catalog;
    production callers never pass them.
    """
    ids = set(commands)
    stale = grandfathered - ids
    if stale:
        raise ValueError(
            "Grandfathered naming entries no longer registered (remove the "
            f"rows): {sorted(stale)}"
        )

    roots_with_subs: set[str] = set()
    roots_with_command: set[str] = set()
    for command_id, cmd in commands.items():
        path: tuple[str, ...] = cmd.path
        if len(path) == 1:
            roots_with_command.add(path[0])
        else:
            roots_with_subs.add(path[0])
            leaf = path[-1]
            if (
                leaf not in CANON_VERBS
                and leaf not in SANCTIONED_LEAF_TOKENS
                and command_id not in grandfathered
            ):
                raise ValueError(
                    f"Command {command_id}: leaf token {leaf!r} is neither a "
                    "canon verb nor a sanctioned domain word. Use a canon "
                    "verb, or record the word in "
                    "command_naming.SANCTIONED_LEAF_TOKENS with a reason "
                    "(style guide: docs/private/command-style-guide.md)."
                )
        for param in cmd.params or ():
            kind = getattr(param, "kind", "")
            name = getattr(param, "name", "")
            if kind == "flag" and name == "global":
                if command_id not in _SCOPE_FLAG_GRANDFATHERED:
                    raise ValueError(
                        f"Command {command_id}: --global flags are retired; "
                        "declare a trailing scope param (kind=\"scope\")."
                    )
            if kind == "option" and name == "scope":
                if command_id not in SCOPE_OPTION_EXCEPTIONS:
                    raise ValueError(
                        f"Command {command_id}: --scope options are retired "
                        "outside the recorded exceptions; declare a trailing "
                        "scope param (kind=\"scope\")."
                    )

    rootless = roots_with_subs - roots_with_command
    unaccounted = (
        rootless - set(ROOTLESS_FAMILY_EXCEPTIONS) - rootless_grandfathered
    )
    if unaccounted:
        raise ValueError(
            "Families with subcommands but no overview root (rule 2 of the "
            f"style guide): {sorted(unaccounted)}"
        )

    unregistered = set(agent_blocked) - roots_with_command - roots_with_subs
    unexplained = unregistered - AGENT_BLOCKED_UNREGISTERED
    if unexplained:
        raise ValueError(
            "AGENT_BLOCKED entries matching no registered root and not "
            f"recorded in AGENT_BLOCKED_UNREGISTERED: {sorted(unexplained)}"
        )
