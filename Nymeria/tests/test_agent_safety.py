"""Tests for NymeriaAgent turn-safety facade dispatch."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from nymeria.core.agent_safety import (
    check_iteration_limit_hit,
    get_effective_hook_enabled,
    get_effective_sequential_tools,
    graph_run_config,
    main_iterations_cap,
    turn_safety_event,
)
from nymeria.vendor.react_agent.nodes import (
    TURN_SAFETY_REASON_REPEATED_TOOL_RESULT,
    TurnSafetyResult,
)


class _AnalyzeFacadeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def _analyze_turn_safety(self, messages: list[Any], max_iterations: int) -> Any:
        self.calls.append(("analyze", messages, max_iterations))
        return SimpleNamespace(should_stop=True)


class _GraphConfigFacadeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def _max_iterations_for_thread(self, thread_id: str | None) -> int:
        self.calls.append(("max_iterations", thread_id))
        return 17

    def _recursion_limit_for_iterations(self, max_iterations: int) -> int:
        self.calls.append(("recursion_limit", max_iterations))
        return 177


class _EventFacadeAgent:
    TURN_SAME_TOOL_RESULT_LIMIT = 5

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def _turn_safety_content(self, safety: TurnSafetyResult) -> str:
        self.calls.append(("content", safety))
        return "facade content"


class _CapAgent:
    MAIN_AGENT_MAX_ITERATIONS = 500

    def __init__(self, settings: Any = None) -> None:
        if settings is not None:
            self.settings = settings


def test_main_iterations_cap_prefers_settings_value():
    agent = _CapAgent(SimpleNamespace(agent_max_iterations=42))
    assert main_iterations_cap(cast(Any, agent)) == 42

    string_agent = _CapAgent(SimpleNamespace(agent_max_iterations="17"))
    assert main_iterations_cap(cast(Any, string_agent)) == 17


def test_main_iterations_cap_falls_back_to_class_constant():
    assert main_iterations_cap(cast(Any, _CapAgent())) == 500

    mock_settings_agent = _CapAgent(MagicMock())
    assert main_iterations_cap(cast(Any, mock_settings_agent)) == 500

    zero_agent = _CapAgent(SimpleNamespace(agent_max_iterations=0))
    assert main_iterations_cap(cast(Any, zero_agent)) == 500

    bool_agent = _CapAgent(SimpleNamespace(agent_max_iterations=True))
    assert main_iterations_cap(cast(Any, bool_agent)) == 500

    garbage_agent = _CapAgent(SimpleNamespace(agent_max_iterations="garbage"))
    assert main_iterations_cap(cast(Any, garbage_agent)) == 500


def test_check_iteration_limit_hit_uses_agent_analyze_facade():
    agent = _AnalyzeFacadeAgent()
    messages = [object()]

    assert check_iteration_limit_hit(cast(Any, agent), messages, 9) is True
    assert agent.calls == [("analyze", messages, 9)]


def test_graph_run_config_uses_agent_facades():
    agent = _GraphConfigFacadeAgent()
    callbacks: list[Any] = ["callback"]

    config = graph_run_config(cast(Any, agent), "thread-a", "user-b", callbacks)

    # The facade agent has no settings/thread_config_manager, so the Tier 2 flag
    # and the tool-timing flag resolve to their defaults (False) and are always
    # present in configurable.
    assert config == {
        "recursion_limit": 177,
        "configurable": {
            "thread_id": "thread-a",
            "user_id": "user-b",
            "sequential_tools": False,
            "tool_timing_in_results": False,
        },
        "callbacks": callbacks,
    }
    assert agent.calls == [
        ("max_iterations", "thread-a"),
        ("recursion_limit", 17),
    ]


def test_graph_run_config_injects_resolved_sequential_flag():
    agent = _GraphConfigFacadeAgent()
    agent.settings = SimpleNamespace(sequential_tool_execution=True)  # type: ignore[attr-defined]
    # No thread_config_manager -> the global default is used as-is.
    config = graph_run_config(cast(Any, agent), "thread-a", "user-b")
    assert config["configurable"]["sequential_tools"] is True


def test_graph_run_config_stamps_tool_timing_flag_from_settings():
    agent = _GraphConfigFacadeAgent()
    agent.settings = SimpleNamespace(tool_timing_in_results=True)  # type: ignore[attr-defined]
    config = graph_run_config(cast(Any, agent), "thread-a", "user-b")
    assert config["configurable"]["tool_timing_in_results"] is True


def test_graph_run_config_stamps_turn_source():
    # slice D5: the turn source is threaded into configurable so a tool hook can
    # scope by autonomous-vs-interactive / holder / trigger.
    agent = _GraphConfigFacadeAgent()
    config = graph_run_config(
        cast(Any, agent),
        "thread-a",
        "user-b",
        hook_is_autonomous=True,
        hook_holder_kind="ticker",
        hook_trigger_label="Scheduled TODO",
    )
    conf = config["configurable"]
    assert conf["hook_is_autonomous"] is True
    assert conf["hook_holder_kind"] == "ticker"
    assert conf["hook_trigger_label"] == "Scheduled TODO"


def test_graph_run_config_stamps_autonomous_false():
    # False is a meaningful value (interactive turn), so it is stamped, not dropped.
    agent = _GraphConfigFacadeAgent()
    config = graph_run_config(
        cast(Any, agent), "thread-a", "user-b", hook_is_autonomous=False
    )
    assert config["configurable"]["hook_is_autonomous"] is False


def test_graph_run_config_omits_turn_source_when_absent():
    # A no-source caller (e.g. a read-only state fetch) leaves the config unchanged.
    agent = _GraphConfigFacadeAgent()
    conf = graph_run_config(cast(Any, agent), "thread-a", "user-b")["configurable"]
    assert "hook_is_autonomous" not in conf
    assert "hook_holder_kind" not in conf
    assert "hook_trigger_label" not in conf


class _SeqManager:
    def __init__(self, override: Any) -> None:
        self._override = override

    def get_config(self, _thread_id: str) -> Any:
        return SimpleNamespace(sequential_tool_execution=self._override)


def test_sequential_resolver_thread_override_beats_global():
    # Thread True beats global False, and thread False beats global True.
    assert get_effective_sequential_tools(False, "t", thread_config_manager=_SeqManager(True)) is True
    assert get_effective_sequential_tools(True, "t", thread_config_manager=_SeqManager(False)) is False


def test_sequential_resolver_none_inherits_global():
    assert get_effective_sequential_tools(True, "t", thread_config_manager=_SeqManager(None)) is True
    assert get_effective_sequential_tools(False, "t", thread_config_manager=_SeqManager(None)) is False


def test_sequential_resolver_no_thread_or_manager_uses_global():
    assert get_effective_sequential_tools(True, None, thread_config_manager=_SeqManager(False)) is True
    assert get_effective_sequential_tools(True, "t", thread_config_manager=None) is True


def test_sequential_resolver_never_raises_on_lookup_failure():
    class _Boom:
        def get_config(self, _tid):
            raise RuntimeError("disk gone")

    assert get_effective_sequential_tools(True, "t", thread_config_manager=_Boom()) is True
    assert get_effective_sequential_tools(False, "t", thread_config_manager=_Boom()) is False


def test_turn_safety_event_uses_agent_content_facade():
    agent = _EventFacadeAgent()
    safety = TurnSafetyResult(
        should_stop=True,
        reason=TURN_SAFETY_REASON_REPEATED_TOOL_RESULT,
        tool_call_count=18,
        max_iterations=17,
        repeated_tool_name="lookup",
        repeated_count=5,
    )

    event = turn_safety_event(cast(Any, agent), safety, scope="callable_thread")

    assert event == {
        "type": "iteration_limit",
        "scope": "callable_thread",
        "reason": TURN_SAFETY_REASON_REPEATED_TOOL_RESULT,
        "content": "facade content",
        "max_iterations": 17,
        "tool_call_count": 18,
        "repeated_tool_name": "lookup",
        "repeated_count": 5,
    }
    assert agent.calls == [("content", safety)]


# --------------------------------------------------------------------------- #
# get_effective_hook_enabled: master switch -> per-thread per-hook -> hook flag
# --------------------------------------------------------------------------- #

def _hook(id_="h1", enabled=True):
    return SimpleNamespace(id=id_, enabled=enabled)


class _TCManager:
    """Thread-config manager stub returning a fixed config object (or None)."""

    def __init__(self, tc: Any) -> None:
        self._tc = tc

    def get_config(self, _thread_id: str) -> Any:
        return self._tc


def _settings(hooks_enabled=True):
    return SimpleNamespace(hooks_enabled=hooks_enabled)


def test_hook_enabled_defaults_to_definition_flag():
    # No thread/manager/settings -> the hook's own flag decides.
    assert get_effective_hook_enabled(_hook(enabled=True), None) is True
    assert get_effective_hook_enabled(_hook(enabled=False), None) is False


def test_hook_enabled_global_master_switch_off_disables_all():
    assert get_effective_hook_enabled(
        _hook(enabled=True), "t", settings=_settings(hooks_enabled=False)
    ) is False


def test_hook_enabled_thread_master_switch_overrides_global():
    # Global on, thread master off -> off.
    tcm = _TCManager(SimpleNamespace(hooks_enabled=False, hook_overrides={}))
    assert get_effective_hook_enabled(
        _hook(enabled=True), "t", thread_config_manager=tcm, settings=_settings(True)
    ) is False
    # Global off, thread master on -> on (thread override beats global).
    tcm2 = _TCManager(SimpleNamespace(hooks_enabled=True, hook_overrides={}))
    assert get_effective_hook_enabled(
        _hook(enabled=True), "t", thread_config_manager=tcm2, settings=_settings(False)
    ) is True


def test_hook_enabled_per_hook_override_beats_definition():
    tcm = _TCManager(SimpleNamespace(hooks_enabled=None, hook_overrides={"h1": False}))
    assert get_effective_hook_enabled(
        _hook(id_="h1", enabled=True), "t", thread_config_manager=tcm, settings=_settings(True)
    ) is False
    tcm2 = _TCManager(SimpleNamespace(hooks_enabled=None, hook_overrides={"h1": True}))
    assert get_effective_hook_enabled(
        _hook(id_="h1", enabled=False), "t", thread_config_manager=tcm2, settings=_settings(True)
    ) is True


def test_hook_enabled_master_off_beats_per_hook_override():
    # Even a per-hook "on" cannot resurrect a hook when the master switch is off.
    tcm = _TCManager(SimpleNamespace(hooks_enabled=False, hook_overrides={"h1": True}))
    assert get_effective_hook_enabled(
        _hook(id_="h1", enabled=True), "t", thread_config_manager=tcm, settings=_settings(True)
    ) is False


def test_hook_enabled_never_raises_on_lookup_failure():
    class _Boom:
        def get_config(self, _tid):
            raise RuntimeError("disk gone")

    # Falls back to the definition flag (master default True) rather than raising.
    assert get_effective_hook_enabled(
        _hook(enabled=True), "t", thread_config_manager=_Boom(), settings=_settings(True)
    ) is True
    assert get_effective_hook_enabled(
        _hook(enabled=False), "t", thread_config_manager=_Boom(), settings=_settings(True)
    ) is False
