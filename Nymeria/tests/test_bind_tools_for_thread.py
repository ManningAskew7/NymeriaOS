"""Characterization tests for ``tool_search.bind_tools_for_thread``.

These lock the exact, byte-for-byte behavior of the tool-binding entry point
before/after the F1+F13 decomposition (slice 12): the result text is asserted
because the agent parses the reload sentinels, and the ``ToolBindingResult``
fields gate downstream callers.

NOTE: several expected strings below are VERBATIM captures of the live function
output, including a pre-existing em dash and a Unicode arrow (``→``) in the
dynamic-binding notice that originate in ``tool_search.py``. Do not "clean them
up" -- the whole point of this file is to detect any change to that output.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria.core.agent import NymeriaAgent, set_current_agent
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.tools import SEED_TOOLS
from nymeria.tools.tool_search import bind_tools_for_thread

# The package re-exports a ``tool_search`` StructuredTool that shadows the
# module attribute; only ``sys.modules`` resolves the real module for patching.
tool_search_module = sys.modules["nymeria.tools.tool_search"]

_SEED_COUNT = len({t.name for t in SEED_TOOLS})


# ---------------------------------------------------------------------------
# Harness (mirrors test_skill_kits._FakeAgent, plus a ``settings`` attribute so
# the dynamic-binding result branch is reachable).
# ---------------------------------------------------------------------------


class _StubSettings:
    def __init__(self, dynamic_tool_binding: bool = False):
        self.dynamic_tool_binding = dynamic_tool_binding


class _FakeRegistry:
    def get_tool(self, name: str):
        return None


class _FakeAgent:
    MAX_TOOL_RELOADS_PER_TURN = 1

    def __init__(self, data_dir: Path, *, role: str = "admin", dynamic: bool = False):
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.tool_registry = _FakeRegistry()
        self._pending_tool_reload: dict = {}
        self._turn_reload_count: dict = {}
        # ``settings`` is load-bearing: should_emit_reload_command() reads
        # ``agent.settings.dynamic_tool_binding`` to pick the legacy-reload vs
        # dynamic-bound result message.
        self.settings = _StubSettings(dynamic_tool_binding=dynamic)
        self.accounts_repo = SimpleNamespace(
            get_user_by_id=lambda user_id: SimpleNamespace(role=role)
        )
        self.profile_manager = SimpleNamespace(
            get_profile=lambda user_id: SimpleNamespace(
                tool_preferences=SimpleNamespace(default_thread_tools=None)
            )
        )
        self.invalidated: list[str] = []

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        self.invalidated.append(thread_id)

    def _resolve_temporary_tools(self, tc):
        return NymeriaAgent._resolve_temporary_tools(self, tc)


@pytest.fixture(autouse=True)
def _reset_current_agent():
    """Never leak the process-global current agent across tests."""
    yield
    set_current_agent(None)


def _agent(tmp_path: Path, **kw) -> _FakeAgent:
    agent = _FakeAgent(tmp_path, **kw)
    set_current_agent(agent)
    return agent


def _dynamic_notice(prior: int, new: int) -> str:
    """Verbatim dynamic-binding notice (em dash + arrow are from the source)."""
    delta = new - prior
    suffix = f" (+{delta} new binding)." if delta > 0 else "."
    return (
        "[Tools bound; callable on the next model step]\n"
        "These tool(s) were NOT in your bound list before this call — "
        "earlier turns of this conversation did not have access to them. "
        "They become callable immediately after this tool result without "
        "a graph rebuild or resume.\n"
        f"Your bound list: {prior} → {new} tool(s)"
        + suffix
        + " Trust this result over any assumption about earlier-turn "
        "availability; do not second-guess this as a redundant enable."
    )


_RELOAD_QUEUED = (
    "[Tool reload queued - STOP NOW]\n"
    "The newly-loaded tools are NOT bound to the model in this "
    "iteration. Do not write a final answer, do not explain the "
    "enablement to the user, and do not call another tool now. This "
    "graph invocation is ending after this tool result so the system "
    "can rebuild the tool list. The system will automatically prompt "
    "you again with tool_reload_resume after the tools are bound; "
    "continue the user's task and call the new tools only after that "
    "automatic resume."
)

_UNLOADABLE_MEMORY = (
    "[Error]: Cannot enable 1 tool(s); they appear in search results but "
    "their backing source is not active:\n"
    "  Other unresolved: memory_clear_all\n"
    "  Names: memory_clear_all"
)


# ---------------------------------------------------------------------------
# Early guards
# ---------------------------------------------------------------------------


def test_no_active_agent_returns_error():
    set_current_agent(None)
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u")
    assert r.ok is False
    assert r.text == "[Error]: No active agent. Cannot modify thread config."
    # pre-ttl guard: dataclass defaults (DEFAULT_TTL / None)
    assert r.ttl_key == "2h"
    assert r.ttl_seconds is None


def test_bad_ttl_returns_error(tmp_path):
    _agent(tmp_path)
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="banana")
    assert r.ok is False
    assert r.text == (
        "[Error]: Invalid tool TTL 'banana'. Format: Nm, Nh, Nd, Nw, or "
        "'never'. Examples: 30m, 2h, 7d, 4w, never."
    )
    assert r.ttl_key == "2h"
    assert r.ttl_seconds is None


def test_unknown_category_returns_error(tmp_path):
    _agent(tmp_path)
    r = bind_tools_for_thread([], "nonsense_cat", "thread-a", "u")
    assert r.ok is False
    assert r.text.startswith("[Error]: Unknown category 'nonsense_cat'. Available: ")
    # post-ttl guard carries the parsed ttl
    assert r.ttl_key == "2h"
    assert r.ttl_seconds == 7200


def test_no_tool_names_returns_error(tmp_path):
    _agent(tmp_path)
    r = bind_tools_for_thread([], "", "thread-a", "u")
    assert r.ok is False
    assert r.text == "[Error]: Provide tool names via 'tools' or a category via 'category'."
    assert r.ttl_seconds == 7200


# ---------------------------------------------------------------------------
# Success paths: each reload-state message
# ---------------------------------------------------------------------------


def _header(*, requested, newly, refreshed, promoted, undisabled, noop, ttl_desc):
    return (
        f"[Success]: {requested} requested. "
        f"{newly} newly loaded, {refreshed} TTL refreshed, {promoted} promoted, "
        f"{undisabled} un-disabled, {noop} no-op "
        f"(TTL for new/refreshed: {ttl_desc})"
    )


def test_newly_added_legacy_reload_queued(tmp_path):
    agent = _agent(tmp_path)  # dynamic=False -> legacy reload
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="2h")
    assert r.ok is True
    assert r.ttl_key == "2h"
    assert r.ttl_seconds == 7200
    assert r.cap_hit is False
    assert r.reload_tools == ["memory_clear_all"]
    expected = (
        _header(requested=1, newly=1, refreshed=0, promoted=0, undisabled=0,
                noop=0, ttl_desc="2h")
        + "\n  Newly loaded: memory_clear_all\n\n"
        + _RELOAD_QUEUED
    )
    assert r.text == expected
    # side effects: pending reload queued, cache invalidated, config persisted
    assert agent._pending_tool_reload["thread-a"]["new_tools"] == ["memory_clear_all"]
    assert agent._pending_tool_reload["thread-a"]["ttl"] == "2h"
    assert agent.invalidated == ["thread-a"]
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "memory_clear_all" in tc.temporary_tools


def test_newly_added_dynamic_bound_message(tmp_path):
    agent = _agent(tmp_path, dynamic=True)
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="2h")
    assert r.ok is True
    assert r.reload_tools == ["memory_clear_all"]
    expected = (
        _header(requested=1, newly=1, refreshed=0, promoted=0, undisabled=0,
                noop=0, ttl_desc="2h")
        + "\n  Newly loaded: memory_clear_all\n\n"
        + _dynamic_notice(_SEED_COUNT, _SEED_COUNT + 1)
    )
    assert r.text == expected
    # dynamic mode does NOT queue a legacy reload
    assert agent._pending_tool_reload == {}


def test_newly_added_permanent_ttl_desc(tmp_path):
    _agent(tmp_path)
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="never")
    assert r.ok is True
    assert r.ttl_key == "never"
    assert r.ttl_seconds is None
    expected = (
        _header(requested=1, newly=1, refreshed=0, promoted=0, undisabled=0,
                noop=0, ttl_desc="permanent")
        + "\n  Newly loaded: memory_clear_all\n\n"
        + _RELOAD_QUEUED
    )
    assert r.text == expected


def test_already_default_noop(tmp_path):
    _agent(tmp_path)  # bash_execute is a SEED member -> default-bound
    r = bind_tools_for_thread(["bash_execute"], "", "thread-a", "u", ttl="2h")
    assert r.ok is True
    assert r.reload_tools == []
    expected = (
        _header(requested=1, newly=0, refreshed=0, promoted=0, undisabled=0,
                noop=1, ttl_desc="2h")
        + "\n  Already bound (default set, no change): bash_execute\n\n"
        + "No binding changes; nothing to reload."
    )
    assert r.text == expected


def test_reload_cap_hit_message(tmp_path):
    agent = _agent(tmp_path)  # legacy mode
    agent._turn_reload_count = {"thread-a": 1}  # already at the cap (1)
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="2h")
    assert r.ok is True
    assert r.cap_hit is True
    expected = (
        _header(requested=1, newly=1, refreshed=0, promoted=0, undisabled=0,
                noop=0, ttl_desc="2h")
        + "\n  Newly loaded: memory_clear_all\n\n"
        + "[Reload cap hit]: this turn has already triggered 1/1 in-turn "
        "graph rebuilds. The new binding was persisted to this thread's "
        "config, but will NOT be bound to the model until the next user "
        "message. Do not attempt to call the newly-enabled tool(s) in this "
        "turn; answer only with that limitation if a response is needed."
    )
    assert r.text == expected
    # cap hit: persisted but no legacy reload queued
    assert agent._pending_tool_reload == {}


# ---------------------------------------------------------------------------
# Classification buckets (sequenced calls)
# ---------------------------------------------------------------------------


def test_ttl_refreshed_bucket(tmp_path):
    _agent(tmp_path, dynamic=True)
    bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="2h")
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="4h")
    assert r.ok is True
    assert r.reload_tools == []
    expected = (
        _header(requested=1, newly=0, refreshed=1, promoted=0, undisabled=0,
                noop=0, ttl_desc="4h")
        + "\n  TTL refreshed: memory_clear_all\n\n"
        + "No binding changes; nothing to reload."
    )
    assert r.text == expected


def test_promoted_bucket(tmp_path):
    _agent(tmp_path, dynamic=True)
    bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="2h")
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="never")
    assert r.ok is True
    expected = (
        _header(requested=1, newly=0, refreshed=0, promoted=1, undisabled=0,
                noop=0, ttl_desc="permanent")
        + "\n  Promoted to permanent: memory_clear_all\n\n"
        + "No binding changes; nothing to reload."
    )
    assert r.text == expected


def test_already_permanent_bucket(tmp_path):
    _agent(tmp_path, dynamic=True)
    bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="never")
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="2h")
    assert r.ok is True
    expected = (
        _header(requested=1, newly=0, refreshed=0, promoted=0, undisabled=0,
                noop=1, ttl_desc="2h")
        + "\n  Already permanent (no change): memory_clear_all\n\n"
        + "No binding changes; nothing to reload."
    )
    assert r.text == expected


def test_un_disabled_bucket(tmp_path):
    agent = _agent(tmp_path, dynamic=True)
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-a", disabled_tools=["memory_clear_all"])
    )
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="2h")
    assert r.ok is True
    assert r.reload_tools == ["memory_clear_all"]
    expected = (
        _header(requested=1, newly=0, refreshed=0, promoted=0, undisabled=1,
                noop=0, ttl_desc="2h")
        + "\n  Un-disabled (removed from disabled list): memory_clear_all\n\n"
        + _dynamic_notice(_SEED_COUNT, _SEED_COUNT + 1)
    )
    assert r.text == expected
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "memory_clear_all" not in tc.disabled_tools


# ---------------------------------------------------------------------------
# Invalid / unloadable / gating / strict / save-failure
# ---------------------------------------------------------------------------


def test_invalid_name_only(tmp_path):
    _agent(tmp_path)
    r = bind_tools_for_thread(["definitely_not_a_real_tool_xyz"], "", "thread-a", "u", ttl="2h")
    assert r.ok is False
    assert r.text == (
        "[Error]: No valid tools to enable. Unknown: definitely_not_a_real_tool_xyz"
    )
    assert r.ttl_seconds == 7200


def test_mixed_valid_and_invalid_dynamic(tmp_path):
    _agent(tmp_path, dynamic=True)
    r = bind_tools_for_thread(
        ["memory_clear_all", "definitely_not_a_real_tool_xyz"], "", "thread-a", "u", ttl="2h"
    )
    assert r.ok is True
    expected = (
        _header(requested=1, newly=1, refreshed=0, promoted=0, undisabled=0,
                noop=0, ttl_desc="2h")
        + "\n  Newly loaded: memory_clear_all"
        + "\n[Not found]: definitely_not_a_real_tool_xyz\n\n"
        + _dynamic_notice(_SEED_COUNT, _SEED_COUNT + 1)
    )
    assert r.text == expected


def test_developer_only_blocked_for_non_admin(tmp_path):
    _agent(tmp_path, role="user")  # hello_test is DEVELOPER_ONLY
    r = bind_tools_for_thread(["hello_test"], "", "thread-a", "u", ttl="2h")
    assert r.ok is False
    assert r.text == (
        "[Error]: Developer-only diagnostic tools cannot be enabled by this "
        "user: ['hello_test']."
    )
    assert r.ttl_seconds == 7200


def test_strict_abort_on_invalid_leaves_config_untouched(tmp_path):
    agent = _agent(tmp_path)
    r = bind_tools_for_thread(
        ["definitely_not_a_real_tool_xyz"], "", "thread-a", "u", ttl="2h", strict=True
    )
    assert r.ok is False
    assert r.text == (
        "[Error]: Tool dependency validation failed; no tools were bound.\n"
        "[Not found]: definitely_not_a_real_tool_xyz"
    )
    assert agent.thread_config_manager.get_config("thread-a") is None


def test_save_failure_rolls_back_and_errors(tmp_path):
    agent = _agent(tmp_path)
    agent.thread_config_manager.save_config = lambda config: False
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="2h")
    assert r.ok is False
    assert r.text == "[Error]: Failed to save thread config."
    assert r.ttl_seconds == 7200


def test_unloadable_only_returns_error(tmp_path, monkeypatch):
    _agent(tmp_path)
    real = tool_search_module._resolve_tool_object
    monkeypatch.setattr(
        tool_search_module,
        "_resolve_tool_object",
        lambda name, agent: None if name == "memory_clear_all" else real(name, agent),
    )
    r = bind_tools_for_thread(["memory_clear_all"], "", "thread-a", "u", ttl="2h")
    assert r.ok is False
    assert r.text == _UNLOADABLE_MEMORY
    assert r.ttl_seconds == 7200


def test_mixed_valid_and_unloadable_success_lists_unloadable(tmp_path, monkeypatch):
    _agent(tmp_path, dynamic=True)
    real = tool_search_module._resolve_tool_object
    monkeypatch.setattr(
        tool_search_module,
        "_resolve_tool_object",
        lambda name, agent: None if name == "memory_clear_all" else real(name, agent),
    )
    # bash_execute resolves (already-default no-op); memory_clear_all unloadable
    r = bind_tools_for_thread(
        ["bash_execute", "memory_clear_all"], "", "thread-a", "u", ttl="2h"
    )
    assert r.ok is True
    expected = (
        _header(requested=1, newly=0, refreshed=0, promoted=0, undisabled=0,
                noop=1, ttl_desc="2h")
        + "\n  Already bound (default set, no change): bash_execute"
        + f"\n[Unloadable]: {_UNLOADABLE_MEMORY}\n\n"
        + "No binding changes; nothing to reload."
    )
    assert r.text == expected


def test_sensitive_tool_emits_warning(tmp_path):
    """If a SENSITIVE non-admin/dev catalog tool exists, enabling it warns."""
    from nymeria.tools import (
        ADMIN_ONLY_TOOL_NAMES,
        DEVELOPER_ONLY_TOOL_NAMES,
        static_tool_catalog,
    )
    from nymeria.tools.metadata import SecurityLevel, get_all_tool_metadata

    sensitive = None
    for name in sorted(static_tool_catalog()):
        if name in ADMIN_ONLY_TOOL_NAMES or name in DEVELOPER_ONLY_TOOL_NAMES:
            continue
        meta = get_all_tool_metadata(name)
        if meta and meta.security_level == SecurityLevel.SENSITIVE:
            sensitive = name
            break
    if sensitive is None:
        pytest.skip("no SENSITIVE non-admin/dev catalog tool available")

    _agent(tmp_path, dynamic=True)
    r = bind_tools_for_thread([sensitive], "", "thread-a", "u", ttl="2h")
    assert r.ok is True
    assert f"[Warning]: '{sensitive}' is SENSITIVE" in r.text
