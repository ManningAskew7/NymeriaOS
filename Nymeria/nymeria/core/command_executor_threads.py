"""Thread-domain command bodies for the central command service.

House style (see ``command_executor_context.py``): new backend handler
families live in their own domain mixin module rather than being appended to
the large ``command_service.py``. ``_CommandExecutor`` inherits
:class:`ThreadCommandsMixin`, which owns the whole ``/thread`` tree (list,
switch, new, delete, info, rename, pin, config, branch, compact) plus the
top-level ``/branch`` alias. The bare ``/thread`` root (context usage) stays on
``_cmd_thread`` in ``command_service.py`` and is intentionally not moved here.

The formatting and resolution helpers are faithful ports of the retired CLI
``triggers/cli/commands/threads.py`` module so the rendered output matches what
the CLI showed before the migration. Client-side effects that a pure text
forwarder would drop (switching the active thread, relabelling it, refreshing
the header) ride back on ``CommandOutput`` state hints instead of the local
``context.dispatch`` calls; the CLI applies them via
``triggers/cli/commands/form_contract.py::apply_state_hints``. Non-CLI
frontends ignore the hints by design.

Nothing is imported from ``command_service`` here, so the module stays a
runtime leaf with no import cycle (``command_service`` imports this module, not
the reverse).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .command_forms import CommandOutput, command_data
from .command_params import BoundArgs

logger = logging.getLogger(__name__)


# ── Small value helpers (self-contained ports of the CLI _shared toolkit) ─────


def _normalize_thread_id(thread: Mapping[str, Any]) -> str:
    """Read a thread ID from either API or in-process payload shapes."""
    return str(thread.get("thread_id") or thread.get("id") or "")


def _thread_title(thread: Mapping[str, Any]) -> str:
    """Return a readable thread title."""
    title = str(thread.get("title") or "").strip()
    return title or "New Chat"


def _compact_id(value: Any, *, width: int = 8) -> str:
    """Return a short display ID."""
    text = str(value or "")
    return text[:width] if len(text) > width else text


def _format_bool(value: Any) -> str:
    """Format booleans for compact command output."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _one_line(value: Any, *, limit: int = 200) -> str:
    """Collapse a value to one bounded display line."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def _mapping_get(data: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    """Read a key from a mapping-like or object-like payload."""
    if isinstance(data, Mapping):
        return data.get(key, default)
    return getattr(data, key, default)


# ── Thread reference resolution ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ThreadResolution:
    """Result of resolving a user-facing thread reference."""

    status: str
    ref: str
    thread: Mapping[str, Any] | None = None
    matches: tuple[Mapping[str, Any], ...] = ()

    @property
    def matched(self) -> bool:
        return self.status == "matched" and self.thread is not None


def resolve_thread_reference(
    threads: Sequence[Mapping[str, Any]],
    ref: str,
) -> ThreadResolution:
    """Resolve a thread by exact ID, unique ID prefix, title, or title substring."""

    needle = " ".join(str(ref or "").split()).strip()
    if not needle:
        return ThreadResolution(status="missing", ref=needle)

    exact_id = [thread for thread in threads if _normalize_thread_id(thread) == needle]
    if exact_id:
        return ThreadResolution(status="matched", ref=needle, thread=exact_id[0])

    prefix = [
        thread for thread in threads if _normalize_thread_id(thread).startswith(needle)
    ]
    if len(prefix) == 1:
        return ThreadResolution(status="matched", ref=needle, thread=prefix[0])
    if len(prefix) > 1:
        return ThreadResolution(status="ambiguous", ref=needle, matches=tuple(prefix))

    exact_title = [thread for thread in threads if _thread_title(thread) == needle]
    if len(exact_title) == 1:
        return ThreadResolution(status="matched", ref=needle, thread=exact_title[0])
    if len(exact_title) > 1:
        return ThreadResolution(
            status="ambiguous", ref=needle, matches=tuple(exact_title)
        )

    folded = needle.casefold()
    substring = [
        thread for thread in threads if folded in _thread_title(thread).casefold()
    ]
    if len(substring) == 1:
        return ThreadResolution(status="matched", ref=needle, thread=substring[0])
    if len(substring) > 1:
        return ThreadResolution(status="ambiguous", ref=needle, matches=tuple(substring))
    return ThreadResolution(status="missing", ref=needle)


def _format_thread_resolution_ambiguity(
    ref: str,
    matches: Sequence[Mapping[str, Any]],
) -> str:
    preview = ", ".join(
        f"{_compact_id(_normalize_thread_id(item))} {_thread_title(item)}"
        for item in matches[:5]
    )
    return f"Ambiguous: {len(matches)} threads match '{ref}': {preview}"


# ── Thread list rendering ─────────────────────────────────────────────────────


def _format_thread_list(
    threads: Sequence[Mapping[str, Any]],
    active_thread_id: str | None,
    *,
    teams: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    by_id = {_normalize_thread_id(thread): thread for thread in threads}
    grouped_ids = _team_thread_ids(teams)
    lines = ["Threads"]

    if teams:
        for team in teams:
            team_threads = _ordered_threads(
                [
                    by_id.get(thread_id)
                    for thread_id in _team_ids(team)
                    if thread_id in by_id
                ]
            )
            if not team_threads:
                continue
            lines.append(f"  Team: {_one_line(team.get('name') or team.get('id'), limit=60)}")
            lines.append("    * ID        Pin  Title                         Platform")
            lines.extend(
                _format_thread_rows(team_threads, active_thread_id, indent="    ")
            )

    ungrouped = [
        thread for thread in threads if _normalize_thread_id(thread) not in grouped_ids
    ]
    pinned = _ordered_threads([thread for thread in ungrouped if thread.get("pinned")])
    recent = _ordered_threads([thread for thread in ungrouped if not thread.get("pinned")])
    if teams:
        if pinned:
            lines.append("  Pinned")
            lines.append("    * ID        Pin  Title                         Platform")
            lines.extend(_format_thread_rows(pinned, active_thread_id, indent="    "))
        if recent:
            lines.append("  Recent")
            lines.append("    * ID        Pin  Title                         Platform")
            lines.extend(_format_thread_rows(recent, active_thread_id, indent="    "))
    else:
        lines.append("  * ID        Pin  Title                         Platform")
        lines.extend(
            _format_thread_rows([*pinned, *recent], active_thread_id, indent="  ")
        )
    return lines


def _ordered_threads(threads: Sequence[Mapping[str, Any] | None]) -> list[Mapping[str, Any]]:
    selected = [thread for thread in threads if isinstance(thread, Mapping)]
    selected.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return selected


def _format_thread_rows(
    threads: Sequence[Mapping[str, Any]],
    active_thread_id: str | None,
    *,
    indent: str,
) -> list[str]:
    rows: list[str] = []
    for thread in threads:
        thread_id = _normalize_thread_id(thread)
        active = "*" if thread_id == active_thread_id else " "
        pin = "*" if thread.get("pinned") else " "
        title = _one_line(_thread_title(thread), limit=28)
        platform = _one_line(thread.get("platform") or "", limit=12)
        rows.append(
            f"{indent}{active} {_compact_id(thread_id):<8}  "
            f"{pin:<3}  {title:<28}  {platform}"
        )
    return rows


def _team_thread_ids(teams: Sequence[Mapping[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for team in teams:
        ids.update(_team_ids(team))
    return ids


def _team_ids(team: Mapping[str, Any]) -> list[str]:
    raw = team.get("thread_ids") or team.get("threadIds") or []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [str(thread_id) for thread_id in raw if str(thread_id)]


# ── Value maps for bound arguments ────────────────────────────────────────────


# Richer than the advertised ``on|off|toggle``, which is why ``/thread pin``
# declares its state param without ``choices``: the dispatcher would reject the
# synonyms this map has always accepted.
_PIN_STATES: dict[str, bool | None] = {
    "on": True,
    "true": True,
    "yes": True,
    "off": False,
    "false": False,
    "no": False,
    "toggle": None,
}


def _resolve_pin_args(
    id_value: str | None,
    state_value: str | None,
    current_thread_id: str | None,
) -> tuple[str | None, bool | None]:
    """Map ``/thread pin``'s two positionals onto (thread ref, desired state).

    The first positional is ambiguous by design: ``/thread pin off`` names a
    state for the active thread, while ``/thread pin nightly`` names a thread
    to toggle. A ``None`` state means toggle.
    """
    if id_value is None:
        return current_thread_id, None
    folded = id_value.casefold()
    if folded in _PIN_STATES:
        return current_thread_id, _PIN_STATES[folded]
    if state_value is None:
        return id_value, None
    return id_value, _PIN_STATES.get(state_value.casefold())


# ── Info / config / compact rendering ────────────────────────────────────────


def _format_thread_info(
    thread_id: str,
    thread: Mapping[str, Any] | None,
    stats: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> str:
    rows = [
        ("Thread ID", thread_id),
        ("Title", _thread_title(thread or {})),
        ("Pinned", _format_bool((thread or {}).get("pinned", False))),
        ("Platform", (thread or {}).get("platform", "")),
        ("Model", _effective_model(stats, config)),
        ("Context", _context_usage(stats)),
        ("Callable", _format_bool((config or {}).get("callable", False))),
        ("Enabled tools", _csv((config or {}).get("enabled_tools"))),
        ("Disabled tools", _csv((config or {}).get("disabled_tools"))),
    ]
    if config and config.get("instructions"):
        rows.append(("Instructions", _one_line(config.get("instructions"), limit=80)))
    return "\n".join(_aligned_rows(rows, "Thread Info"))


def _format_thread_config(config: Mapping[str, Any]) -> str:
    llm_config = config.get("llm_config") or {}
    if not isinstance(llm_config, Mapping):
        llm_config = {}
    rows = [
        ("Thread ID", config.get("thread_id", "")),
        ("Callable", _format_bool(config.get("callable", False))),
        ("Callable name", config.get("callable_name", "") or ""),
        ("Instructions", _one_line(config.get("instructions"), limit=80)),
        ("System prompt", _one_line(config.get("system_prompt"), limit=80)),
        ("Enabled tools", _csv(config.get("enabled_tools"))),
        ("Disabled tools", _csv(config.get("disabled_tools"))),
        ("Enabled skills", _csv(config.get("enabled_skills"))),
        ("Disabled skills", _csv(config.get("disabled_skills"))),
        ("LLM provider", llm_config.get("provider", "") or ""),
        ("LLM model", llm_config.get("model", "") or ""),
    ]
    return "\n".join(_aligned_rows(rows, "Thread Config"))


def _compact_result_text(result: Any) -> str:
    if isinstance(result, Mapping) and result.get("success") is False:
        reason = result.get("reason", "Unknown reason")
        return f"[Info]: Skipped: {reason}"
    removed = _mapping_get(result, "messages_removed", None)
    before = _mapping_get(result, "messages_before", None)
    after = _mapping_get(result, "messages_after", None)
    if removed is not None:
        detail = f" Removed {removed} messages"
        if before is not None and after is not None:
            detail += f" ({before} -> {after})"
        detail += "."
    else:
        detail = "."
    return f"[Success]: Compaction requested{detail}"


def _effective_model(
    stats: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> str:
    llm_config = (config or {}).get("llm_config") or {}
    if isinstance(llm_config, Mapping) and llm_config.get("model"):
        return str(llm_config["model"])
    return str(stats.get("model") or stats.get("active_model") or "")


def _context_usage(stats: Mapping[str, Any]) -> str:
    total = stats.get("total_tokens", stats.get("used_tokens", ""))
    limit = stats.get("context_limit", stats.get("max_tokens", ""))
    pct = stats.get("usage_percentage")
    parts = []
    if total != "":
        parts.append(f"{total:,}" if isinstance(total, int) else str(total))
    if limit != "":
        rendered_limit = f"{limit:,}" if isinstance(limit, int) else str(limit)
        parts.append(f"/ {rendered_limit}")
    if pct is not None:
        parts.append(f"({pct}%)")
    return " ".join(parts)


def _csv(value: Any) -> str:
    if not value:
        return "None"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return ", ".join(str(item) for item in value) or "None"
    return str(value)


def _aligned_rows(rows: Sequence[tuple[str, Any]], title: str) -> list[str]:
    width = max((len(label) for label, _value in rows), default=0)
    lines = [title]
    for label, value in rows:
        lines.append(f"  {label:<{width}}  {'' if value is None else value}")
    return lines


class ThreadCommandsMixin:
    """Thread-domain command bodies mixed into ``_CommandExecutor``.

    The host (:class:`nymeria.core.command_service._CommandExecutor`) provides
    ``api``, ``thread_id``, ``user_id`` and ``_require_thread``; the annotations
    below let the static checker see them on the mixin in isolation.
    """

    api: Any
    thread_id: str
    user_id: str

    if TYPE_CHECKING:
        def _require_thread(self) -> str | None: ...

    # ── Shared internals ──────────────────────────────────────────────────

    async def _list_threads(self) -> list[Mapping[str, Any]]:
        threads = await self.api.list_threads(self.user_id)
        if not isinstance(threads, Sequence) or isinstance(threads, (str, bytes)):
            return []
        return [thread for thread in threads if isinstance(thread, Mapping)]

    async def _thread_teams(self) -> list[Mapping[str, Any]]:
        # Both command backends expose list_thread_teams; guard defensively for
        # any slimmer test double that does not.
        if not callable(getattr(self.api, "list_thread_teams", None)):
            return []
        raw = await self.api.list_thread_teams(self.user_id)
        if isinstance(raw, Mapping):
            raw = raw.get("teams", [])
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return []
        return [team for team in raw if isinstance(team, Mapping)]

    async def _resolve_thread(
        self,
        ref: str,
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        """Resolve a ref to a thread mapping, or a legacy error/info string."""

        threads = await self._list_threads()
        resolution = resolve_thread_reference(threads, ref)
        if resolution.matched:
            assert resolution.thread is not None
            return resolution.thread, None
        if resolution.status == "missing":
            return None, f"[Error]: No thread matching '{ref}'."
        return None, "[Info]: " + _format_thread_resolution_ambiguity(
            ref, resolution.matches
        )

    # ── Read commands ─────────────────────────────────────────────────────

    async def _cmd_thread_list(self, bound: BoundArgs) -> str:
        threads = await self._list_threads()
        teams = await self._thread_teams()
        if not threads:
            return "[Info]: No threads found."
        lines = _format_thread_list(threads, self.thread_id or None, teams=teams)
        return "[Info]: " + "\n".join(lines)

    async def _cmd_thread_info(self, bound: BoundArgs) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error

        thread: Mapping[str, Any] | None = None
        for candidate in await self._list_threads():
            if _normalize_thread_id(candidate) == self.thread_id:
                thread = candidate
                break

        stats = await self.api.get_context_stats(self.thread_id)
        stats = stats if isinstance(stats, Mapping) else {}
        config = await self.api.get_thread_config(self.thread_id)
        config = config if isinstance(config, Mapping) else None
        return "[Info]: " + _format_thread_info(self.thread_id, thread, stats, config)

    async def _cmd_thread_config(self, bound: BoundArgs) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        config = await self.api.get_thread_config(self.thread_id)
        if not isinstance(config, Mapping):
            return "[Info]: No thread config found."
        return "[Info]: " + _format_thread_config(config)

    # ── /team read commands (backlog #100 phase 2) ────────────────────────

    async def _cmd_team(self, bound: BoundArgs) -> str:
        # Registry dispatch routes "/team list" and "/team show ..." to the
        # dedicated handlers; anything else lands here. Bare "/team" lists,
        # "/team <ref>" is show shorthand.
        ref = str(bound.get("team") or "").strip()
        if not ref:
            return await self._cmd_team_list(BoundArgs())
        return await self._show_team(ref)

    async def _cmd_team_list(self, bound: BoundArgs) -> str:
        teams = await self._thread_teams()
        if not teams:
            return (
                "[Info]: No callable-thread teams yet. Create one from the "
                "desktop sidebar or ask the agent to use team_manage."
            )
        lines = [f"Teams ({len(teams)}):"]
        for team in teams:
            member_count = len(team.get("thread_ids") or [])
            description = str(team.get("description") or "").strip()
            suffix = f" {description}" if description else ""
            lines.append(
                f"- {team.get('name')} ({team.get('id')}): "
                f"{member_count} member(s).{suffix}"
            )
        return "[Info]: " + "\n".join(lines)

    async def _cmd_team_show(self, bound: BoundArgs) -> str:
        return await self._show_team(str(bound.get("team") or "").strip())

    async def _show_team(self, ref: str) -> str:
        """Render one team, resolved by id then by case-insensitive name.

        Shared by "/team show <ref>" and the "/team <ref>" shorthand, so the
        two cannot drift.
        """
        teams = await self._thread_teams()
        match: Mapping[str, Any] | None = None
        for team in teams:
            if str(team.get("id") or "") == ref:
                match = team
                break
        if match is None:
            folded = ref.casefold()
            for team in teams:
                if str(team.get("name") or "").strip().casefold() == folded:
                    match = team
                    break
        if match is None:
            names = ", ".join(str(t.get("name")) for t in teams) or "(none)"
            return f"[Error]: No team matching '{ref}'. Teams: {names}"
        titles = {
            _normalize_thread_id(thread): _thread_title(thread)
            for thread in await self._list_threads()
        }
        lines = [f"Team: {match.get('name')}", f"Id: {match.get('id')}"]
        description = str(match.get("description") or "").strip()
        if description:
            lines.append(f"Description: {description}")
        member_ids = [str(t) for t in (match.get("thread_ids") or [])]
        lines.append(f"Members ({len(member_ids)}):")
        for member_id in sorted(member_ids):
            title = titles.get(member_id) or ""
            lines.append(f"- {member_id} {title}".rstrip())
        if not member_ids:
            lines.append("- (none)")
        return "[Info]: " + "\n".join(lines)

    # ── Navigation / creation (ride switch_thread state hints) ────────────

    async def _cmd_thread_switch(self, bound: BoundArgs) -> str | CommandOutput:
        match, error = await self._resolve_thread(
            str(bound.get("id_or_title") or "").strip()
        )
        if error:
            return error
        assert match is not None
        thread_id = _normalize_thread_id(match)
        title = _thread_title(match)
        return CommandOutput(
            f"[Success]: Switched to {_compact_id(thread_id)} {title}",
            data=command_data(
                state={"switch_thread": {"thread_id": thread_id, "thread_label": title}}
            ),
        )

    async def _cmd_thread_new(self, bound: BoundArgs) -> str | CommandOutput:
        title = str(bound.get("title") or "").strip() or None
        created = await self.api.create_thread(self.user_id, title=title)
        if not isinstance(created, Mapping):
            return "[Error]: Thread create response was not a mapping."
        thread_id = _normalize_thread_id(created)
        if not thread_id:
            return "[Error]: Thread create response did not include a thread ID."
        selected_title = _thread_title(created)
        return CommandOutput(
            f"[Success]: New thread: {thread_id} {selected_title}",
            data=command_data(
                state={
                    "switch_thread": {
                        "thread_id": thread_id,
                        "thread_label": selected_title,
                    }
                }
            ),
        )

    async def _cmd_thread_branch(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        from_message_index = bound.get("from")
        if from_message_index is not None and from_message_index < 1:
            return "[Error]: --from must be 1 or greater"
        # Repeatable positional: join the collected words (--from parses on
        # either side of the title, matching the retired hand parser).
        title = " ".join(bound.get("title") or []).strip() or None

        result = await self.api.branch_thread(
            self.thread_id,
            self.user_id,
            title=title,
            from_message_index=from_message_index,
        )
        if not isinstance(result, Mapping):
            return "[Error]: Thread branch response was not a mapping."
        thread_id = _normalize_thread_id(result)
        if not thread_id:
            return "[Error]: Thread branch response did not include a thread ID."
        selected_title = _thread_title(result)
        copied_from = result.get("from_message_index")
        suffix = f" from message #{copied_from}" if copied_from else ""
        return CommandOutput(
            f"[Success]: Created branch '{selected_title}' "
            f"({_compact_id(thread_id)}){suffix}.",
            data=command_data(
                state={
                    "switch_thread": {
                        "thread_id": thread_id,
                        "thread_label": selected_title,
                    }
                }
            ),
        )

    async def _cmd_branch(self, bound: BoundArgs) -> str | CommandOutput:
        """Top-level ``/branch`` (alias ``/fork``); same as ``/thread branch``."""
        return await self._cmd_thread_branch(bound)

    # ── Metadata edits (ride label / metadata refresh hints) ──────────────

    async def _cmd_thread_rename(self, bound: BoundArgs) -> str | CommandOutput:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        title = str(bound.get("title") or "").strip()
        await self.api.update_thread_metadata(self.thread_id, self.user_id, title=title)
        return CommandOutput(
            f"[Success]: Renamed to: {title}",
            data=command_data(state={"thread_label": title}),
        )

    async def _cmd_thread_pin(self, bound: BoundArgs) -> str | CommandOutput:
        thread_ref, desired = _resolve_pin_args(
            bound.get("id"), bound.get("state"), self.thread_id or None
        )
        if not thread_ref:
            return "[Error]: No active thread; name the thread to pin by id or title."

        match, error = await self._resolve_thread(thread_ref)
        if error:
            return error
        assert match is not None
        thread_id = _normalize_thread_id(match)
        current = bool(match.get("pinned", False))
        pinned = not current if desired is None else desired

        await self.api.update_thread_metadata(thread_id, self.user_id, pinned=pinned)
        state = (
            {"thread_metadata_updated": True} if thread_id == self.thread_id else None
        )
        return CommandOutput(
            f"[Success]: {'Pinned' if pinned else 'Unpinned'} {_compact_id(thread_id)}.",
            data=command_data(state=state),
        )

    # ── Destructive / mutating ────────────────────────────────────────────

    async def _cmd_thread_delete(self, bound: BoundArgs) -> str:
        # The declared ``--yes`` flag is accepted for CLI muscle memory but no
        # longer prompts, so nothing here reads it: backend handlers cannot
        # prompt, so the danger_level metadata drives any frontend confirmation
        # instead (mirrors ``/hook delete``).
        match, error = await self._resolve_thread(str(bound.get("id") or ""))
        if error:
            return error
        assert match is not None
        thread_id = _normalize_thread_id(match)
        if thread_id == self.thread_id:
            return "[Error]: Cannot delete the active thread. Switch first."
        await self.api.delete_thread(thread_id, self.user_id)
        return f"[Success]: Deleted thread {_compact_id(thread_id)}."

    async def _cmd_thread_compact(self, bound: BoundArgs) -> str | CommandOutput:
        # ``--yes`` is declared and ignored for the same reason as
        # ``/thread delete``: no backend handler can prompt.
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        result = await self.api.compact_thread(self.thread_id, self.user_id)
        return CommandOutput(
            _compact_result_text(result),
            data=command_data(state={"thread_context_updated": True}),
        )
