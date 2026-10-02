"""Backlog #164: an existing account learns which core tools shipped after it.

Seeding is first-time-only by design (no backfill), so an account whose
``default_thread_tools`` predates a seed promotion never got the tool and
nothing told it the tool existed. The fix is record-backed and read-only:

- ``SEED_TOOL_PROMOTED`` dates each seed tool (the ratchet below fails the
  build when a promotion forgets its date, E10);
- ``ToolPreferences.declined_core_tools`` is the explicit record of a seed tool
  the account removed or dismissed, so a deliberate removal is never offered
  as new;
- "new" = promoted after the profile's ``created_at``, not in the defaults,
  not declined, allowed by role.

This file pins the pure status rule and the command surface end to end
(the real serializer and writer behind the in-process command client). The
HTTP routes, the unified payload, the agent's prompt and the wizard mirror
are pinned beside their existing tests (test_api_tools_router.py,
test_api_unified_tools_router.py, test_agent_prompt.py,
test_setup_wizard_core_tools.py).

Edges skipped on purpose: concurrency (every write is one
``atomic_update``, pinned by #400's tests) and unicode names (tool names are
a closed registry).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import nymeria.tools as tools_pkg
from nymeria.core.accounts import AccountsRepo
from nymeria.core.command_service import (
    CommandBackendClient,
    CommandContext,
    CommandService,
    _CommandBackendUser,
)
from nymeria.core.thread_config import ThreadConfigManager
from nymeria.core.user_profile import UserProfileManager
from nymeria.tools import (
    SEED_TOOL_PROMOTED,
    SEED_TOOLS,
    core_seed_tool_names,
    core_tool_statuses,
    fresh_default_thread_tool_names,
)

# The newest promotion is the natural "new" fixture: a profile created the
# day before it was offered nothing promoted after that day except it. The
# floor tool is one every profile was offered (a curated removal, neutral).
# ``.get`` so a seed tool missing its date fails the ratchet below rather
# than this module's import.
NEWEST = max(core_seed_tool_names(), key=lambda name: SEED_TOOL_PROMOTED.get(name, date.min))
FLOOR_TOOL = "notify"
BEFORE = datetime.combine(
    SEED_TOOL_PROMOTED[NEWEST] - timedelta(days=1), datetime.min.time(), timezone.utc
)
AFTER = datetime.combine(
    SEED_TOOL_PROMOTED[NEWEST] + timedelta(days=1), datetime.min.time(), timezone.utc
)


def _without(*names: str) -> list[str]:
    return [name for name in fresh_default_thread_tool_names() if name not in names]


def _new(*args: Any) -> list[str]:
    return [name for name, status in core_tool_statuses(*args).items() if status == "new"]


# E10: the ledger ratchet


def test_every_seed_tool_carries_a_promotion_date():
    """A tool promoted into SEED_TOOLS without a date fails the build.

    Without its date a promotion is invisible to every existing account
    (the gap this item closes); a stale key for a tool that left the seed
    would mislabel it if it ever came back without a fresh date.
    """
    assert set(SEED_TOOL_PROMOTED) == {t.name for t in SEED_TOOLS}


def test_promotion_dates_are_real_days_in_the_past():
    floor = date(2026, 2, 7)  # the initial commit
    today = datetime.now(timezone.utc).date()
    for name, promoted in SEED_TOOL_PROMOTED.items():
        assert isinstance(promoted, date), name
        assert floor <= promoted <= today, (name, promoted)


# the status rule (pure)


def test_a_tool_promoted_after_the_profile_was_created_is_new():
    statuses = core_tool_statuses(_without(NEWEST, FLOOR_TOOL), [], BEFORE, "user")

    assert statuses[NEWEST] == "new"
    # Already in the seed when the profile was made: a removal, never "new".
    assert statuses[FLOOR_TOOL] == "absent"
    assert {statuses[n] for n in core_seed_tool_names() if n not in (NEWEST, FLOOR_TOOL)} == {
        "default"
    }
    assert _new(_without(NEWEST, FLOOR_TOOL), [], BEFORE, "user") == [NEWEST]


def test_the_same_tool_reads_absent_for_a_profile_created_after_it():
    assert core_tool_statuses(_without(NEWEST), [], AFTER, "user")[NEWEST] == "absent"


def test_a_profile_created_on_the_promotion_day_reads_it_as_offered():
    """Day granularity, and the tie goes to "offered" (the fail-quiet side)."""
    same_day = datetime.combine(
        SEED_TOOL_PROMOTED[NEWEST], datetime.max.time(), timezone.utc
    )
    assert core_tool_statuses(_without(NEWEST), [], same_day, "user")[NEWEST] == "absent"


def test_a_declined_tool_is_never_new_and_one_in_the_list_is_default_whatever_the_record():
    declined = core_tool_statuses(_without(NEWEST), [NEWEST], BEFORE, "user")
    back_on = core_tool_statuses(fresh_default_thread_tool_names(), [NEWEST], BEFORE, "user")

    assert declined[NEWEST] == "declined"
    assert back_on[NEWEST] == "default"


def test_a_missing_created_at_reads_as_now_so_nothing_is_new():
    assert _new(_without(NEWEST), [], None, "user") == []


def test_a_naive_created_at_is_read_as_utc():
    naive = BEFORE.replace(tzinfo=None)
    assert core_tool_statuses(_without(NEWEST), [], naive, "user")[NEWEST] == "new"


def test_a_fresh_account_has_nothing_new():
    """E1: a profile seeded today with the stock defaults."""
    now = datetime.now(timezone.utc)
    assert _new(fresh_default_thread_tool_names(), [], now, "user") == []
    statuses = core_tool_statuses(fresh_default_thread_tool_names(), [], now, "user")
    assert set(statuses.values()) == {"default"}


def test_a_seed_tool_the_role_may_not_have_is_left_out(monkeypatch):
    """No seed tool is role-gated today; the rule must hold the day one is."""
    monkeypatch.setattr(tools_pkg, "ADMIN_ONLY_TOOL_NAMES", frozenset({NEWEST}))

    as_user = core_tool_statuses(_without(NEWEST), [], BEFORE, "user")
    as_admin = core_tool_statuses(_without(NEWEST), [], BEFORE, "admin")

    assert NEWEST not in as_user
    assert as_admin[NEWEST] == "new"


# the command surface, end to end


class _Agent:
    """The managers the real serializer and writer read, on a temp data dir."""

    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.profile_manager = UserProfileManager(data_dir)
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.default_graph_rebuilds = 0

    def _rebuild_default_graphs(self) -> None:
        self.default_graph_rebuilds += 1


def _account(tmp_path: Path, *, defaults: list[str], created_at: datetime, declined=()):
    agent = _Agent(tmp_path)
    agent.accounts_repo.create_user("alice", "alice@example.com", "Alice", role="user")
    with agent.profile_manager.atomic_update("alice") as profile:
        profile.created_at = created_at
        profile.tool_preferences.default_thread_tools = list(defaults)
        profile.tool_preferences.declined_core_tools = list(declined)
    backend = CommandBackendClient(agent, user=_CommandBackendUser(id="alice", role="user"))
    return agent, backend


def _run(api: Any, line: str):
    ctx = CommandContext(
        user_id="alice", thread_id="thread-1", actor="user", surface="cli", is_admin=False
    )
    return asyncio.run(CommandService().execute(ctx, line, api=api))


def _section(markdown: str, heading: str) -> list[str]:
    """The tool names listed under ``heading`` (empty when it is absent)."""
    lines = markdown.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(heading)]
    if not starts:
        return []
    names = []
    for line in lines[starts[0] + 1:]:
        if not line.startswith("  ") or line.strip().startswith("Turn one on"):
            break
        names.append(line.strip().split(":")[0])
    return names


NEW_HEADING = "New since this account was set up"
OTHER_HEADING = "Other standard tools not in your defaults"


def test_list_core_names_a_new_tool_with_how_to_enable_or_dismiss_it(tmp_path):
    """E2 and E6 on the command surface."""
    _agent, backend = _account(
        tmp_path, defaults=_without(NEWEST, FLOOR_TOOL), created_at=BEFORE
    )

    result = _run(backend, "/tools list core")

    assert result.success is True, result.markdown
    assert _section(result.markdown, NEW_HEADING) == [NEWEST]
    assert "`/tools enable <name> global`" in result.markdown
    assert "`/tools disable <name> global`" in result.markdown
    # A seed tool this account was offered and lacks is neutral, never new.
    assert _section(result.markdown, OTHER_HEADING) == [FLOOR_TOOL]
    assert f"Core Tools: {len(_without(NEWEST, FLOOR_TOOL))} tools" in (
        result.markdown.splitlines()[0]
    )


def test_list_core_has_no_discovery_sections_for_a_fresh_account(tmp_path):
    """E1."""
    _agent, backend = _account(
        tmp_path,
        defaults=fresh_default_thread_tool_names(),
        created_at=datetime.now(timezone.utc),
    )

    result = _run(backend, "/tools list core")

    assert NEW_HEADING not in result.markdown
    assert OTHER_HEADING not in result.markdown


def test_disabling_an_absent_new_tool_globally_records_the_decline(tmp_path):
    """E4: was a silent no-op that answered "Removed"; now the decline is
    recorded, the tool leaves "new" for the neutral section, and the list
    itself is untouched (nothing enabled, nothing removed)."""
    agent, backend = _account(
        tmp_path, defaults=_without(NEWEST, FLOOR_TOOL), created_at=BEFORE
    )

    disabled = _run(backend, f"/tools disable {NEWEST} global")
    listing = _run(backend, "/tools list core")

    assert disabled.success is True, disabled.markdown
    assert "recorded as declined" in disabled.markdown
    prefs = agent.profile_manager.get_profile("alice").tool_preferences
    assert prefs.declined_core_tools == [NEWEST]
    assert sorted(prefs.default_thread_tools) == sorted(_without(NEWEST, FLOOR_TOOL))
    assert _section(listing.markdown, NEW_HEADING) == []
    assert sorted(_section(listing.markdown, OTHER_HEADING)) == sorted([FLOOR_TOOL, NEWEST])


def test_enabling_a_declined_tool_globally_adds_it_and_clears_the_decline(tmp_path):
    """E4's second half: a declined tool turned back on stays on."""
    agent, backend = _account(
        tmp_path, defaults=_without(NEWEST), created_at=BEFORE, declined=[NEWEST]
    )

    enabled = _run(backend, f"/tools enable {NEWEST} global")

    assert enabled.success is True, enabled.markdown
    prefs = agent.profile_manager.get_profile("alice").tool_preferences
    assert NEWEST in prefs.default_thread_tools
    assert prefs.declined_core_tools == []


def test_removing_a_new_tool_after_enabling_it_never_brings_the_badge_back(tmp_path):
    """E5 through the command surface: enable, then disable globally."""
    agent, backend = _account(tmp_path, defaults=_without(NEWEST), created_at=BEFORE)

    _run(backend, f"/tools enable {NEWEST} global")
    removed = _run(backend, f"/tools disable {NEWEST} global")
    listing = _run(backend, "/tools list core")

    assert "Removed tool" in removed.markdown
    prefs = agent.profile_manager.get_profile("alice").tool_preferences
    assert NEWEST not in prefs.default_thread_tools
    assert prefs.declined_core_tools == [NEWEST]
    assert _section(listing.markdown, NEW_HEADING) == []


def test_disabling_a_non_seed_tool_that_is_off_records_nothing(tmp_path):
    """The decline record is for standard tools only, and an off catalog tool
    is reported as unchanged instead of "Removed"."""
    agent, backend = _account(tmp_path, defaults=_without(NEWEST), created_at=BEFORE)
    before = agent.profile_manager.get_profile("alice").tool_preferences.model_dump()

    result = _run(backend, "/tools disable web_search_brave global")

    assert result.success is True, result.markdown
    assert "nothing changed" in result.markdown
    after = agent.profile_manager.get_profile("alice").tool_preferences.model_dump()
    assert after == before
    assert agent.default_graph_rebuilds == 0


def test_reads_never_write_the_profile_or_enable_anything(tmp_path):
    """E7: the listing and the read model are pure reads."""
    agent, backend = _account(
        tmp_path, defaults=_without(NEWEST, FLOOR_TOOL), created_at=BEFORE
    )
    path = tmp_path / "users" / "alice" / "profile.json"
    agent.profile_manager.get_profile("alice")  # settle any lazy migration
    before = path.read_bytes()

    payload = asyncio.run(backend.get_default_tools("alice"))
    _run(backend, "/tools list core")

    assert payload["new_core_tools"] == [NEWEST]
    assert path.read_bytes() == before
    assert agent.default_graph_rebuilds == 0


class _ThreadApi:
    """The real defaults read and write, plus a fixed thread config: bare
    `/tools` also reads the thread, whose ownership door is not under test."""

    def __init__(self, backend: CommandBackendClient):
        self._backend = backend

    async def get_default_tools(self, user_id: str = "default") -> dict:
        return await self._backend.get_default_tools(user_id)

    async def get_thread_config(self, thread_id: str, user_id: str | None = None) -> dict:
        return {"enabled_tools": [], "disabled_tools": [], "temporary_tools": {}}


def test_bare_tools_adds_one_line_only_when_something_is_new(tmp_path):
    """E2's bare `/tools` line, and E1's absence of it."""
    _agent, backend = _account(tmp_path / "a", defaults=_without(NEWEST), created_at=BEFORE)
    _fresh, fresh_backend = _account(
        tmp_path / "b",
        defaults=fresh_default_thread_tool_names(),
        created_at=datetime.now(timezone.utc),
    )

    with_new = _run(_ThreadApi(backend), "/tools")
    without_new = _run(_ThreadApi(fresh_backend), "/tools")

    notice = [line for line in with_new.markdown.splitlines() if "New standard tools" in line]
    assert notice == [
        f"New standard tools since this account was set up: {NEWEST} "
        "(`/tools list core` to review)."
    ]
    assert "New standard tools" not in without_new.markdown


def test_the_in_process_payload_carries_the_status_on_seed_items_only(tmp_path):
    """Seed rows carry ``core_status``; the ~1,250 catalog rows do not."""
    _agent, backend = _account(tmp_path, defaults=_without(NEWEST), created_at=BEFORE)

    payload = asyncio.run(backend.get_default_tools("alice"))

    by_name = {item["name"]: item for item in payload["available_tools"]}
    assert by_name[NEWEST]["core_status"] == "new"
    assert by_name[FLOOR_TOOL]["core_status"] == "default"
    catalog_rows = [i for i in payload["available_tools"] if i["is_optional"]]
    assert catalog_rows and all("core_status" not in item for item in catalog_rows)
