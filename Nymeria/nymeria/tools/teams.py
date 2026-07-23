"""Agent-facing callable-team management (backlog #100 phase 2).

One action-dispatch CATALOG tool, ``team_manage``, over the shared team
services in ``core/team_manager.py`` (the same functions behind the REST
routes and the ``nym.threads.configure`` verb, so the surfaces cannot
drift). Own-user scope only: every action resolves against the acting
user's store and owned threads; human gating for sensitive setups rides
the ``pre_tool_use`` hook substrate as usual.
"""

import logging
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .registry import ToolGroup, register_tool_group
from .utils import current_agent, get_user_id

logger = logging.getLogger(__name__)


def _resolve_member_thread(agent, user_id: str, ref: str):
    """Resolve a thread ref (id or callable name) to an OWNED ThreadConfig.

    Reuses the shared owned-thread resolver behind ``nym.threads.configure``;
    returns ``(tc, None)`` or ``(None, error_string)``.
    """
    from ..core.workflows.verbs_thread import VerbError, resolve_owned_thread

    try:
        return resolve_owned_thread(agent, user_id, ref, caller="team_manage"), None
    except VerbError as e:
        return None, f"[Error]: {e}"


def _member_label(agent, thread_id: str) -> str:
    tc = agent.thread_config_manager.get_config(thread_id)
    callable_name = getattr(tc, "callable_name", None) if tc else None
    return f"{thread_id} (callable: {callable_name})" if callable_name else thread_id


def _render_team_show(agent, user_id: str, team) -> str:
    members = agent.team_manager.members(user_id, team.id)
    lines = [f"[Team]: {team.name}", f"Id: {team.id}"]
    if team.description:
        lines.append(f"Description: {team.description}")
    lines.append(f"Members ({len(members)}):")
    for thread_id in sorted(members):
        lines.append(f"- {_member_label(agent, thread_id)}")
    if not members:
        lines.append("- (none; empty teams are legal)")
    lines.append(f"Team memory keys: {len(team.memories)}")
    return "\n".join(lines)


@tool
def team_manage(
    action: str,
    team: Optional[str] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    thread: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Manage your callable-thread teams (isolated invocation bubbles).

    Teams control which callable threads can invoke each other: threads in
    the same team see only each other's callables, and unteamed threads see
    only unteamed callables. Membership changes apply from each affected
    thread's next turn. Actions via the `action` parameter:

      action="list": All your teams with member counts.
      action="show": One team in detail (members, description, memory keys).
      action="create": Create a team (empty; add members with add_thread).
      action="rename": Rename a team. O(1); members are untouched.
      action="describe": Set or clear (empty string) a team's description.
      action="add_thread": Move a thread into the team (leaving any
          previous team).
      action="remove_thread": Remove a thread from the team (it becomes
          unteamed).
      action="delete": Delete the team; its members become unteamed.

    Args:
        action: One of list, show, create, rename, describe, add_thread,
            remove_thread, delete.
        team: Team reference (team id or display name). Required for every
            action except list and create.
        name: New team display name. Required for create and rename.
            Max 120 chars; names are unique per user (case-insensitive).
        description: Optional free-text description (max 2000 chars). Used
            by create and describe; empty string clears it.
        thread: Thread reference (thread id or callable thread name) you
            own. Required for add_thread and remove_thread.

    Returns:
        Human-readable result lines; errors start with "[Error]:" and name
        the fix.
    """
    agent = current_agent()
    if agent is None:
        return "[Error]: No active agent; cannot manage teams."
    manager = getattr(agent, "team_manager", None)
    if manager is None:
        return "[Error]: Team management is unavailable on this runtime."

    from ..core.team_manager import (
        after_team_change,
        resolve_team_ref,
        serialize_thread_teams,
        set_thread_team,
    )

    user_id = get_user_id(config)
    action_key = (action or "").strip().lower()
    if action_key not in (
        "list",
        "show",
        "create",
        "rename",
        "describe",
        "add_thread",
        "remove_thread",
        "delete",
    ):
        return (
            "[Error]: action must be one of: list, show, create, rename, "
            "describe, add_thread, remove_thread, delete."
        )

    if action_key == "list":
        teams = serialize_thread_teams(agent, user_id)["teams"]
        if not teams:
            return (
                "[Info]: You have no teams yet. Create one with "
                'team_manage(action="create", name="...").'
            )
        lines = [f"[Teams]: {len(teams)}"]
        for entry in teams:
            desc = f" {entry['description']}" if entry.get("description") else ""
            lines.append(
                f"- {entry['name']} ({entry['id']}): "
                f"{len(entry['thread_ids'])} member(s).{desc}"
            )
        return "\n".join(lines)

    if action_key == "create":
        if not (name or "").strip():
            return "[Error]: create requires name=."
        try:
            created = manager.create_team(
                user_id, name=name, description=description
            )
        except (ValueError, RuntimeError) as e:
            return f"[Error]: {e}"
        after_team_change(
            agent,
            user_id,
            membership_changed=False,
            team_id=created.id,
            reason="created",
        )
        return (
            f"[Success]: Created team '{created.name}' (id: {created.id}). "
            f'Add members with team_manage(action="add_thread", '
            f'team="{created.name}", thread="<thread id or callable name>").'
        )

    # Every remaining action targets an existing team. show is read-only, so
    # it must not adopt a dangling id into the store (no write on a read
    # path); mutation actions adopt to keep the store coherent.
    if not (team or "").strip():
        return f"[Error]: {action_key} requires team= (team id or name)."
    target = resolve_team_ref(
        agent, user_id, team or "", adopt=action_key != "show"
    )
    if target is None:
        return (
            f"[Error]: Unknown team '{team}'. "
            'See team_manage(action="list") for your teams.'
        )

    if action_key == "show":
        return _render_team_show(agent, user_id, target)

    if action_key == "rename":
        if not (name or "").strip():
            return "[Error]: rename requires name=."
        try:
            renamed = manager.rename_team(user_id, target.id, name)
        except (ValueError, RuntimeError) as e:
            return f"[Error]: {e}"
        if renamed is None:
            return f"[Error]: Unknown team '{team}'."
        after_team_change(
            agent,
            user_id,
            membership_changed=False,
            renamed=True,
            team_id=target.id,
            reason="renamed",
        )
        return (
            f"[Success]: Renamed team to '{renamed.name}' (id: {renamed.id}). "
            "No member thread was touched."
        )

    if action_key == "describe":
        try:
            updated = manager.describe_team(user_id, target.id, description)
        except RuntimeError as e:
            return f"[Error]: {e}"
        if updated is None:
            return f"[Error]: Unknown team '{team}'."
        after_team_change(
            agent,
            user_id,
            membership_changed=False,
            team_id=target.id,
            reason="described",
        )
        if updated.description:
            return f"[Success]: Updated description for team '{updated.name}'."
        return f"[Success]: Cleared description for team '{updated.name}'."

    if action_key in ("add_thread", "remove_thread"):
        if not (thread or "").strip():
            return (
                f"[Error]: {action_key} requires thread= "
                "(a thread id or callable thread name you own)."
            )
        tc, error = _resolve_member_thread(agent, user_id, thread or "")
        if error or tc is None:
            return error or f"[Error]: Could not resolve thread '{thread}'."
        current_team_id = getattr(tc, "callable_team_id", None) or None
        if action_key == "add_thread":
            if current_team_id == target.id:
                return (
                    f"[Info]: Thread {tc.thread_id} is already in team "
                    f"'{target.name}'."
                )
            try:
                set_thread_team(agent, user_id, tc.thread_id, target.id)
            except RuntimeError as e:
                return f"[Error]: {e}"
            after_team_change(
                agent,
                user_id,
                membership_changed=True,
                team_id=target.id,
                reason="membership",
            )
            return (
                f"[Success]: Thread {tc.thread_id} joined team "
                f"'{target.name}'. Applies from its next turn."
            )
        if current_team_id != target.id:
            return (
                f"[Info]: Thread {tc.thread_id} is not a member of team "
                f"'{target.name}'."
            )
        try:
            set_thread_team(agent, user_id, tc.thread_id, None)
        except RuntimeError as e:
            return f"[Error]: {e}"
        after_team_change(
            agent,
            user_id,
            membership_changed=True,
            team_id=target.id,
            reason="membership",
        )
        return (
            f"[Success]: Thread {tc.thread_id} left team '{target.name}' "
            "and is now unteamed."
        )

    # action_key == "delete" (the allowlist above makes this exhaustive).
    members = manager.members(user_id, target.id)
    unteamed_ok: list[str] = []
    for member_thread_id in sorted(members):
        try:
            set_thread_team(agent, user_id, member_thread_id, None)
        except RuntimeError as e:
            # Honest partial-failure accounting: earlier members are already
            # unteamed ON DISK, so their graphs must still be invalidated and
            # clients nudged; the entity is kept so a retry can finish.
            if unteamed_ok:
                after_team_change(
                    agent,
                    user_id,
                    membership_changed=True,
                    team_id=target.id,
                    reason="membership",
                )
            already = (
                f" {len(unteamed_ok)} member thread(s) were already unteamed "
                f"({', '.join(unteamed_ok)});"
                if unteamed_ok
                else ""
            )
            return (
                f"[Error]: {e}.{already} team '{target.name}' was NOT "
                "deleted. Retry the delete to finish."
            )
        unteamed_ok.append(member_thread_id)
    try:
        manager.delete_team(user_id, target.id)
    except RuntimeError as e:
        after_team_change(
            agent,
            user_id,
            membership_changed=bool(unteamed_ok),
            team_id=target.id,
            reason="membership",
        )
        return (
            f"[Error]: {e}. All {len(unteamed_ok)} member thread(s) were "
            f"unteamed but the team entity remains. Retry the delete."
        )
    after_team_change(
        agent,
        user_id,
        membership_changed=bool(members),
        team_id=target.id,
        reason="deleted",
    )
    unteamed = (
        f" {len(members)} member thread(s) are now unteamed." if members else ""
    )
    return f"[Success]: Deleted team '{target.name}'.{unteamed}"


# Grouped export for CATALOG_TOOLS registration (opt-in).
TEAM_TOOLS = [team_manage]

# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="teams", tools=tuple(TEAM_TOOLS)))
