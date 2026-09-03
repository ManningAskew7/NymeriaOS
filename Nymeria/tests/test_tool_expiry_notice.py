"""Kit TTL expiry awareness (backlog #320, #319, #232).

Three notices make a lapsed kit TTL honest instead of silent: the once-only
``[System: ...]`` line under the next prompt's metadata block, the same text
absorbed mid-turn through the queued-prompt path, and an unbound-call refusal
that names the kit and recommends only reachable remedies. The durable carrier
is ``ThreadConfig.expired_tools``, written by the eviction and cleared by a
re-bind; ``notified`` is the single once-only truth across both deliveries.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.core.agent import set_current_agent
from nymeria.core.agent_history import format_conversation_history, strip_prompt_context
from nymeria.core.agent_streaming_input import (
    _apply_tool_expiry_notice,
    prepare_astream_input,
)
from nymeria.core.agent_tools import (
    TOOL_EXPIRY_NOTICE_CAP,
    consume_tool_expiry_notice,
    expiry_clock,
    record_tool_expiries,
    render_tool_expiry_notice,
    resolve_temporary_tools,
)
from nymeria.core.agent_turn_loops import build_queued_prompt_messages
from nymeria.core.pending_prompt_queue import (
    get_pending_queue,
    make_pending_prompt,
    reset_pending_queue_for_tests,
)
from nymeria.core.thread_config import (
    EXPIRED_TOOL_RECORD_LIMIT,
    ExpiredToolEntry,
    TemporaryToolEntry,
    ThreadConfig,
    ThreadConfigManager,
)
from nymeria.core.time_utils import format_user_time, utc_now
from nymeria.tools.tool_search import bind_tools_for_thread
from nymeria.vendor.react_agent.nodes import (
    SafeToolNode,
    route_after_tools,
    unbound_call_refusal,
)

# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _MemoryManager:
    """In-memory thread-config store with disk semantics: reads hand out a
    copy, so only ``save_config`` makes a change stick, and a save can be
    told to fail."""

    def __init__(self, config: Optional[ThreadConfig] = None) -> None:
        self.config = config
        self.save_ok = True
        self.saves = 0

    def get_config(self, thread_id: str) -> Optional[ThreadConfig]:
        if self.config is None or self.config.thread_id != thread_id:
            return None
        return self.config.model_copy(deep=True)

    def save_config(self, config: ThreadConfig) -> bool:
        if not self.save_ok:
            return False
        self.saves += 1
        self.config = config.model_copy(deep=True)
        return True

    def delete_config(self, thread_id: str) -> bool:
        self.config = None
        return True


class _FakeRegistry:
    def get_tool(self, name: str):
        return None


class _Agent:
    MAX_TOOL_RELOADS_PER_TURN = 1

    def __init__(self, manager: Any, *, role: str = "admin") -> None:
        self.thread_config_manager = manager
        self._tool_expiry_signal: set[str] = set()
        self.tool_registry = _FakeRegistry()
        self._pending_tool_reload: dict = {}
        self._turn_reload_count: dict = {}
        self.settings = SimpleNamespace(dynamic_tool_binding=True)
        self.accounts_repo = SimpleNamespace(
            get_user_by_id=lambda user_id: SimpleNamespace(role=role)
        )
        self.profile_manager = SimpleNamespace(
            get_profile=lambda user_id: SimpleNamespace(
                tool_preferences=SimpleNamespace(default_thread_tools=None)
            )
        )
        self.skill_manager: Any = None

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        pass

    def _resolve_temporary_tools(self, tc, *, persist: bool = True):
        return resolve_temporary_tools(self, tc, persist=persist)

    def should_halt_for_subturn_compaction(self, thread_id, messages) -> bool:
        return False

    def _max_iterations_for_thread(self, thread_id):
        return None


def _entry(
    *, expires_in: int, source: Optional[str] = None, kit: Optional[str] = None,
    enabled_ago: int = 3600,
) -> TemporaryToolEntry:
    now = utc_now()
    return TemporaryToolEntry(
        enabled_at=now - timedelta(seconds=enabled_ago),
        expires_at=now + timedelta(seconds=expires_in),
        source=source,
        kit=kit,
    )


def _record(
    *, kit: Optional[str] = None, source: Optional[str] = "skill_kit",
    ttl_seconds: int = 3600, expired_ago: int = 600, notified: bool = False,
) -> ExpiredToolEntry:
    now = utc_now()
    expired_at = now - timedelta(seconds=expired_ago)
    return ExpiredToolEntry(
        expired_at=expired_at,
        enabled_at=expired_at - timedelta(seconds=ttl_seconds),
        source=source,
        kit=kit,
        notified=notified,
    )


KIT_RECORDS = {
    "tool_search": _record(kit="tool-management"),
    "tool_manage": _record(kit="tool-management"),
}


@pytest.fixture
def pending_queue():
    reset_pending_queue_for_tests()
    yield get_pending_queue()
    reset_pending_queue_for_tests()


@pytest.fixture
def no_agent():
    set_current_agent(None)
    yield
    set_current_agent(None)


# ---------------------------------------------------------------------------
# 1. Entry provenance and the expiry record
# ---------------------------------------------------------------------------


def test_temporary_entry_provenance_round_trips(tmp_path: Path):
    manager = ThreadConfigManager(tmp_path)
    tc = ThreadConfig(
        thread_id="t",
        temporary_tools={"tool_search": _entry(expires_in=3600, source="skill_kit", kit="tool-management")},
    )
    assert manager.save_config(tc)
    loaded = manager.get_config("t")
    assert loaded is not None
    entry = loaded.temporary_tools["tool_search"]
    assert (entry.source, entry.kit) == ("skill_kit", "tool-management")
    # Entries written before the fields existed load with no provenance.
    legacy = ThreadConfig.model_validate(
        {"thread_id": "t", "temporary_tools": {"x": {"expires_at": utc_now().isoformat()}}}
    )
    assert (legacy.temporary_tools["x"].source, legacy.temporary_tools["x"].kit) == (None, None)


def test_expired_record_alone_is_a_customization():
    assert not ThreadConfig(thread_id="t").has_customizations()
    tc = ThreadConfig(thread_id="t", expired_tools={"x": _record()})
    assert tc.has_customizations()
    assert not tc.expired_tools["x"].notified


def test_eviction_records_the_lapse_and_flags_the_thread():
    manager = _MemoryManager()
    agent = _Agent(manager)
    lapsed = _entry(expires_in=-5, source="skill_kit", kit="tool-management")
    tc = ThreadConfig(
        thread_id="t",
        temporary_tools={"tool_search": lapsed, "exa_search": _entry(expires_in=3600)},
    )

    live = resolve_temporary_tools(agent, tc)

    assert live == {"exa_search"}
    record = tc.expired_tools["tool_search"]
    assert (record.kit, record.source) == ("tool-management", "skill_kit")
    assert record.expired_at == lapsed.expires_at
    assert record.enabled_at == lapsed.enabled_at
    assert record.notified is False
    saved = manager.get_config("t")
    assert saved is not None and "tool_search" in saved.expired_tools
    assert "tool_search" not in saved.temporary_tools
    assert agent._tool_expiry_signal == {"t"}


def test_read_only_resolve_records_nothing():
    manager = _MemoryManager()
    agent = _Agent(manager)
    tc = ThreadConfig(thread_id="t", temporary_tools={"x": _entry(expires_in=-5)})

    assert resolve_temporary_tools(agent, tc, persist=False) == set()

    assert tc.expired_tools == {}
    assert "x" in tc.temporary_tools
    assert manager.saves == 0
    assert agent._tool_expiry_signal == set()


def test_expiry_records_age_out_and_cap():
    stale = _record(expired_ago=8 * 86400)
    tc = ThreadConfig(thread_id="t", expired_tools={"stale": stale})
    evicted = {
        f"tool_{i:03d}": TemporaryToolEntry(
            enabled_at=utc_now() - timedelta(hours=2),
            expires_at=utc_now() - timedelta(seconds=i),
        )
        for i in range(EXPIRED_TOOL_RECORD_LIMIT + 10)
    }

    record_tool_expiries(tc, evicted)

    assert "stale" not in tc.expired_tools
    assert len(tc.expired_tools) == EXPIRED_TOOL_RECORD_LIMIT
    # The newest lapses survive (smallest ``i`` expired most recently).
    assert "tool_000" in tc.expired_tools
    assert f"tool_{EXPIRED_TOOL_RECORD_LIMIT + 9:03d}" not in tc.expired_tools


def test_expiry_records_age_out_below_the_cap():
    # Nowhere near the cap, so only the 7-day age-out can drop a record
    # (the cap test above would pass with the age-out disabled).
    tc = ThreadConfig(
        thread_id="t",
        expired_tools={
            "stale": _record(expired_ago=8 * 86400),
            "recent": _record(expired_ago=6 * 86400),
        },
    )
    fresh = TemporaryToolEntry(
        enabled_at=utc_now() - timedelta(hours=2),
        expires_at=utc_now() - timedelta(seconds=5),
    )

    record_tool_expiries(tc, {"fresh": fresh})

    assert set(tc.expired_tools) == {"recent", "fresh"}


# ---------------------------------------------------------------------------
# 2. The bind path stamps provenance and clears the record
# ---------------------------------------------------------------------------


def _bind(agent, tools, **kwargs):
    set_current_agent(agent)  # type: ignore[arg-type]
    try:
        return bind_tools_for_thread(tools, "", "t", "u", **kwargs)
    finally:
        set_current_agent(None)


def test_kit_bind_stamps_kit_provenance(tmp_path: Path):
    agent = _Agent(ThreadConfigManager(tmp_path))
    result = _bind(
        agent, ["tool_search"], ttl="1h", strict=True,
        source="skill_kit", skill_name="tool-management",
    )
    assert result.ok is True
    entry = agent.thread_config_manager.get_config("t").temporary_tools["tool_search"]
    assert (entry.source, entry.kit) == ("skill_kit", "tool-management")


def test_direct_refresh_overwrites_kit_provenance(tmp_path: Path):
    agent = _Agent(ThreadConfigManager(tmp_path))
    _bind(agent, ["tool_search"], ttl="1h", strict=True, source="skill_kit", skill_name="tool-management")
    first = agent.thread_config_manager.get_config("t").temporary_tools["tool_search"]

    result = _bind(agent, ["tool_search"], ttl="4h", source="tool_manage")

    assert result.ok is True
    entry = agent.thread_config_manager.get_config("t").temporary_tools["tool_search"]
    assert (entry.source, entry.kit) == ("tool_manage", None)
    assert entry.enabled_at == first.enabled_at  # a refresh keeps the original bind time
    assert entry.expires_at > first.expires_at


def test_rebind_clears_the_expiry_record(tmp_path: Path):
    manager = ThreadConfigManager(tmp_path)
    manager.save_config(ThreadConfig(
        thread_id="t",
        expired_tools={"tool_search": _record(kit="tool-management"), "other": _record()},
    ))
    agent = _Agent(manager)

    result = _bind(agent, ["tool_search"], ttl="1h", source="tool_manage")

    assert result.ok is True
    records = manager.get_config("t").expired_tools
    assert "tool_search" not in records
    assert "other" in records  # unrelated lapses stay on file


# ---------------------------------------------------------------------------
# 3. The notice text and its once-only consumption
# ---------------------------------------------------------------------------


def test_notice_groups_by_kit_then_direct_binds():
    records = {
        **KIT_RECORDS,
        "exa_search": _record(kit=None, source="tool_manage", ttl_seconds=1800),
    }
    text = render_tool_expiry_notice(records)

    assert text.startswith("[System: ") and text.endswith("]")
    assert text.count("]") == 1  # bracket-free inside: the strip frame is exact
    assert "Skill Kit tool-management's tools expired at" in text
    assert "(1h TTL)" in text
    assert "no longer bound: tool_manage, tool_search." in text
    assert 'Re-activate it with Skill(name="tool-management") (or /kit tool-management)' in text
    assert "TTL also expired on: exa_search (bound by tool_manage); re-bind with tool_manage" in text


def test_notice_without_kits_leads_with_the_direct_lapse():
    text = render_tool_expiry_notice({"exa_search": _record(kit=None, source="tool_manage")})
    assert text.startswith("[System: TTL expired on: exa_search (bound by tool_manage)")
    assert "Skill Kit" not in text


def test_notice_names_collapse_within_one_kit():
    records = {f"a_very_long_tool_name_{i:03d}": _record(kit="big-kit") for i in range(60)}
    text = render_tool_expiry_notice(records)
    assert "and 50 more" in text
    assert text.count("]") == 1


def test_notice_truncates_when_many_kits_blow_the_cap():
    # One kit can never reach the cap after the name collapse; many kits
    # each contribute a full sentence, so this is the only path that
    # exercises truncation, and the frame must survive it intact (the
    # history strip pattern keys on exactly one closing bracket).
    records = {f"tool_{i:02d}": _record(kit=f"kit-number-{i:02d}") for i in range(20)}
    text = render_tool_expiry_notice(records)
    assert text.endswith("...]")
    assert len(text) <= TOOL_EXPIRY_NOTICE_CAP + len("[System: ]")
    assert text.count("]") == 1


def test_expiry_clock_names_the_day_once_the_lapse_is_not_today():
    recent = utc_now() - timedelta(minutes=1)
    assert re.fullmatch(r"\d{2}:\d{2} [AP]M( \(.*\))?", expiry_clock(recent))
    old = utc_now() - timedelta(days=2)
    assert expiry_clock(old) == format_user_time(old)
    assert " at " in expiry_clock(old)


def test_consume_ages_out_delivered_records_so_the_thread_stops_reading_customized():
    manager = _MemoryManager(ThreadConfig(
        thread_id="t",
        expired_tools={"old": _record(expired_ago=8 * 86400, notified=True)},
    ))
    agent = _Agent(manager)

    assert consume_tool_expiry_notice(agent, "t") is None  # type: ignore[arg-type]

    assert manager.config is not None
    assert manager.config.expired_tools == {}
    assert manager.config.has_customizations() is False


def test_prefix_path_evicts_a_lapsed_entry_itself():
    # No graph lookup ran the resolver this turn: the prefix path must still
    # record the lapse and announce it.
    manager = _MemoryManager(ThreadConfig(
        thread_id="t",
        temporary_tools={"tool_search": _entry(expires_in=-5, kit="tool-management", source="skill_kit")},
    ))
    agent = _Agent(manager)
    human = HumanMessage(content="hi")

    _apply_tool_expiry_notice(agent, "t", human)  # type: ignore[arg-type]

    assert human.content.startswith("[System: Skill Kit tool-management")
    assert manager.config is not None
    assert manager.config.temporary_tools == {}
    assert manager.config.expired_tools["tool_search"].notified is True


def test_kit_provenance_accepts_a_long_kit_name(tmp_path: Path):
    name = "kit-" + "x" * 200
    entry = _entry(expires_in=60, kit=name, source="skill_kit")
    assert ExpiredToolEntry.from_temporary(entry).kit == name


def test_consume_flips_once_and_withholds_on_save_failure():
    manager = _MemoryManager(ThreadConfig(thread_id="t", expired_tools=dict(KIT_RECORDS)))
    agent = _Agent(manager)

    peek = consume_tool_expiry_notice(agent, "t", commit=False)
    assert peek and peek.startswith("[System: Skill Kit tool-management")
    assert not any(r.notified for r in manager.config.expired_tools.values())

    first = consume_tool_expiry_notice(agent, "t")
    assert first == peek
    assert all(r.notified for r in manager.config.expired_tools.values())
    assert consume_tool_expiry_notice(agent, "t") is None

    # A later lapse is announced on its own; the delivered ones stay quiet.
    manager.config.expired_tools["exa_search"] = _record(kit=None, source="tool_manage")
    later = consume_tool_expiry_notice(agent, "t")
    assert later is not None and "exa_search" in later and "tool-management" not in later

    manager.config.expired_tools["fresh"] = _record(kit="other-kit")
    manager.save_ok = False
    assert consume_tool_expiry_notice(agent, "t") is None
    assert manager.config.expired_tools["fresh"].notified is False


# ---------------------------------------------------------------------------
# 4. Delivery on the next prompt: under the metadata block, stripped from history
# ---------------------------------------------------------------------------


def _agent_with_records(**records) -> _Agent:
    return _Agent(_MemoryManager(ThreadConfig(thread_id="t", expired_tools=records)))


def test_prefix_notice_lands_under_the_metadata_block():
    agent = _agent_with_records(**KIT_RECORDS)
    block = "[Time: Monday, June 01, 2026 at 09:00 AM (UTC)]\n[Trigger: User Message]\n\n"
    msg = HumanMessage(content=block + "hello")

    _apply_tool_expiry_notice(agent, "t", msg)

    notice = msg.additional_kwargs["tool_expiry_notice"]
    assert notice.startswith("[System: Skill Kit tool-management")
    assert msg.content == f"{block}{notice}\n\nhello"
    assert strip_prompt_context(msg.content) == "hello"
    # Once-only: the same records do not fire twice.
    again = HumanMessage(content=block + "next")
    _apply_tool_expiry_notice(agent, "t", again)
    assert again.content == block + "next"


def test_prefix_notice_heads_a_message_without_a_metadata_block():
    agent = _agent_with_records(**KIT_RECORDS)
    msg = HumanMessage(content="hello")
    _apply_tool_expiry_notice(agent, "t", msg)
    assert msg.content.startswith("[System: ") and msg.content.endswith("]\n\nhello")
    assert strip_prompt_context(msg.content) == "hello"


def test_prefix_notice_enters_the_leading_text_block_of_image_content():
    agent = _agent_with_records(**KIT_RECORDS)
    msg = HumanMessage(content=[
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ])
    _apply_tool_expiry_notice(agent, "t", msg)
    assert msg.content[0]["text"].startswith("[System: ")
    assert msg.content[0]["text"].endswith("]\n\nlook")
    assert msg.content[1]["type"] == "image_url"


def test_prepare_astream_input_applies_the_notice():
    agent = _agent_with_records(**KIT_RECORDS)
    input_state, _summary, err = prepare_astream_input(
        agent,  # type: ignore[arg-type]
        message_with_context="hello",
        thread_id="t",
        image_attachments=None,
        sandbox_records=None,
        force_unsupported_attachments=False,
        is_self_invoke=False,
    )
    assert err is None
    msg = input_state["messages"][0]
    assert msg.content.startswith("[System: Skill Kit tool-management")
    assert msg.content.endswith("hello")
    assert "tool_expiry_notice" in msg.additional_kwargs


# ---------------------------------------------------------------------------
# 5. Delivery mid-turn: the queued-prompt absorb path
# ---------------------------------------------------------------------------

_ROUTE_CONFIG = {"configurable": {"thread_id": "t", "user_id": "u"}}


def test_mid_turn_notice_rides_the_pending_queue(pending_queue, no_agent):
    agent = _agent_with_records(**KIT_RECORDS)
    agent._tool_expiry_signal.add("t")
    set_current_agent(agent)  # type: ignore[arg-type]

    assert route_after_tools({"messages": []}, _ROUTE_CONFIG) == "end"

    batch = pending_queue.drain("t")
    assert len(batch) == 1
    prompt = batch[0]
    assert (prompt.source, prompt.user_id, prompt.is_autonomous) == ("system", "u", True)
    assert prompt.message.startswith("[System: Skill Kit tool-management")
    # The enqueue is a peek: nothing is delivered yet.
    assert not any(r.notified for r in agent.thread_config_manager.config.expired_tools.values())
    assert agent._tool_expiry_signal == set()
    injected = build_queued_prompt_messages(batch, agent=agent, thread_id="t")[0]
    assert injected.additional_kwargs.get("internal") is True
    assert injected.additional_kwargs.get("internal_type") == "tool_expiry_notice"
    assert "[Trigger: System Notice]" in injected.content
    assert injected.content.endswith(prompt.message)
    # Absorb is the commit: the records are delivered now, and the next
    # prompt carries nothing.
    assert all(r.notified for r in agent.thread_config_manager.config.expired_tools.values())
    human = HumanMessage(content="next")
    _apply_tool_expiry_notice(agent, "t", human)
    assert human.content == "next"


def test_stop_between_enqueue_and_absorb_keeps_the_notice_for_the_next_prompt(
    pending_queue, no_agent
):
    agent = _agent_with_records(**KIT_RECORDS)
    agent._tool_expiry_signal.add("t")
    set_current_agent(agent)  # type: ignore[arg-type]
    assert route_after_tools({"messages": []}, _ROUTE_CONFIG) == "end"
    # A user stop drops non-user prompts without absorbing them.
    pending_queue.drain("t")

    human = HumanMessage(content="after the stop")
    _apply_tool_expiry_notice(agent, "t", human)

    assert human.content.startswith("[System: Skill Kit tool-management")


def test_absorb_skips_a_system_prompt_whose_records_were_already_delivered(no_agent):
    agent = _agent_with_records(
        tool_search=_record(kit="tool-management", notified=True)
    )
    system = make_pending_prompt(
        message="[System: stale]", source="system", source_id=None,
        source_label="System notice", user_id="u", is_autonomous=True,
    )
    user = make_pending_prompt(
        message="hello", source="user", source_id=None,
        source_label="User", user_id="u", is_autonomous=False,
    )

    built = build_queued_prompt_messages([system, user], agent=agent, thread_id="t")

    assert len(built) == 1 and built[0].content.endswith("hello")


def test_mid_turn_without_a_signal_queues_nothing(pending_queue, no_agent):
    agent = _agent_with_records(**KIT_RECORDS)
    set_current_agent(agent)  # type: ignore[arg-type]

    assert route_after_tools({"messages": []}, _ROUTE_CONFIG) != "end"

    assert pending_queue.size("t") == 0
    assert not any(r.notified for r in agent.thread_config_manager.config.expired_tools.values())


def test_mid_turn_signal_without_records_clears_itself(pending_queue, no_agent):
    agent = _Agent(_MemoryManager(ThreadConfig(thread_id="t")))
    agent._tool_expiry_signal.add("t")
    set_current_agent(agent)  # type: ignore[arg-type]

    assert route_after_tools({"messages": []}, _ROUTE_CONFIG) != "end"

    assert pending_queue.size("t") == 0
    assert agent._tool_expiry_signal == set()


def test_mid_turn_closing_queue_keeps_the_records_for_the_next_prompt(pending_queue, no_agent):
    agent = _agent_with_records(**KIT_RECORDS)
    agent._tool_expiry_signal.add("t")
    set_current_agent(agent)  # type: ignore[arg-type]
    pending_queue.begin_release("t")

    assert route_after_tools({"messages": []}, _ROUTE_CONFIG) != "end"

    assert pending_queue.size("t") == 0
    assert not any(r.notified for r in agent.thread_config_manager.config.expired_tools.values())


# ---------------------------------------------------------------------------
# 6. The refusal names the kit and only reachable remedies
# ---------------------------------------------------------------------------


def _refusal(name, *, effective, agent=None, thread_id="t", user_id="u") -> str:
    return unbound_call_refusal(
        name, effective_names=set(effective), agent=agent, thread_id=thread_id, user_id=user_id
    )


def test_refusal_names_the_expired_kit_and_the_recall_path():
    agent = _agent_with_records(**KIT_RECORDS)
    text = _refusal("tool_search", effective={"Skill", "tool_manage", "tool_invoke"}, agent=agent)

    assert "'tool_search' is no longer bound on this thread" in text
    assert "Skill Kit 'tool-management' bound it with a 1h TTL that expired at" in text
    assert 'Skill(name="tool-management")' in text
    assert 'tool_manage(action="enable", tools=["tool_search"]' in text
    # Protected: the one-off route is never offered, and the reason is stated.
    assert "tool_invoke(" not in text
    assert "protected management tool" in text


def test_refusal_offers_the_user_kit_command_when_skill_is_unbound():
    agent = _agent_with_records(**KIT_RECORDS)
    text = _refusal("tool_manage", effective=set(), agent=agent)
    assert "ask the user to run /kit tool-management" in text
    assert "Skill(name=" not in text
    assert "tool_manage(action=" not in text


def test_refusal_for_a_direct_ttl_lapse_names_the_binder():
    agent = _agent_with_records(exa_search=_record(kit=None, source="tool_manage", ttl_seconds=1800))
    text = _refusal("exa_search", effective={"tool_manage", "tool_invoke"}, agent=agent)
    assert "tool_manage bound it with a 30m TTL that expired at" in text
    assert 'tool_manage(action="enable", tools=["exa_search"]' in text
    assert 'tool_invoke(name="exa_search"' in text
    assert "protected" not in text


def test_refusal_names_an_installed_kit_that_provides_the_tool():
    agent = _Agent(_MemoryManager(ThreadConfig(thread_id="t")))
    agent.skill_manager = SimpleNamespace(
        list_installed=lambda user_id=None: [
            SimpleNamespace(name="browser-control", required_tools=["chrome_act", "chrome_read_page"]),
            SimpleNamespace(name="plain-skill", required_tools=[]),
        ]
    )
    text = _refusal("chrome_act", effective={"Skill"}, agent=agent)
    assert "Skill Kit 'browser-control' provides it" in text
    assert 'Skill(name="browser-control")' in text


def test_refusal_with_no_reachable_remedy_says_so():
    agent = _Agent(_MemoryManager(ThreadConfig(thread_id="t")))
    text = _refusal("tool_manage", effective={"tool_invoke"}, agent=agent)
    assert "no binding tool is bound on this thread; ask the user to run /tools enable tool_manage" in text
    assert "tool_invoke(" not in text  # protected: offering it would be a dead pointer
    assert "protected management tool" in text


def test_refusal_without_an_agent_keeps_the_plain_shape():
    text = _refusal("exa_search", effective={"tool_manage", "tool_invoke"}, agent=None)
    assert text.startswith("[Error]: 'exa_search' is not enabled on this thread, so it cannot be called directly.")
    assert "tool_manage(action=" in text and "tool_invoke(" in text


def test_safe_tool_node_strict_refusal_reads_the_thread_record(no_agent):
    from langchain_core.tools import tool

    @tool
    def tool_search(query: str) -> str:
        """Stand-in for the protected management tool."""
        return query

    @tool
    def bnd_echo(text: str) -> str:
        """Bound tool."""
        return text

    agent = _agent_with_records(**KIT_RECORDS)
    set_current_agent(agent)  # type: ignore[arg-type]
    node = SafeToolNode([bnd_echo, tool_search], dynamic_tool_resolver=lambda: ([bnd_echo], "h1"))
    node._ensure_dynamic_tools_for_calls([])

    msg = node._unbound_call_message(
        {"name": "tool_search", "args": {"query": "x"}, "id": "c1"}, _ROUTE_CONFIG
    )

    assert msg is not None and msg.status == "error"
    assert "Skill Kit 'tool-management' bound it" in msg.content
    assert re.search(r"expired at \d{2}:\d{2} [AP]M", msg.content)


# --- history projection: humans see what the model saw ----------------------

NOTICE = (
    "[System: Skill Kit tool-management's tools expired at 10:00 AM (2h TTL) "
    "and are no longer bound: tool_search.]"
)
META = "[Time: Thursday 2026-09-03 10:05 AM UTC]\n[Trigger: Desktop]"


def _prefixed_human(msg_id: str = "h1") -> HumanMessage:
    return HumanMessage(
        content=f"{META}\n\n{NOTICE}\n\nhello",
        additional_kwargs={"tool_expiry_notice": NOTICE},
        id=msg_id,
    )


def _tool_turn(ai_id: str, tool_id: str) -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": "echo", "args": {}, "id": tool_id}], id=ai_id),
        ToolMessage(content="ok", tool_call_id=tool_id, id=f"{tool_id}-r"),
    ]


def test_history_lifts_the_next_turn_notice_into_a_card_before_the_prompt():
    history = format_conversation_history(
        [_prefixed_human(), AIMessage(content="done", id="a1")], thread_id="t"
    )
    assert [(e["role"], e.get("kind")) for e in history] == [
        ("system", "tool_expiry_notice"),
        ("user", None),
        ("assistant", None),
    ]
    assert history[0]["content"] == NOTICE and "message_id" not in history[0]
    assert history[1]["content"] == "hello" and history[1]["message_id"] == "h1"
    ordinals = [int(e["id"].rsplit("-", 1)[1]) for e in history]
    assert ordinals == sorted(ordinals)


def test_history_notice_on_a_hidden_wakeup_still_gets_its_card():
    wakeup = HumanMessage(
        content=f"{META}\n\n{NOTICE}\n\n[Scheduled TODO] check mail",
        additional_kwargs={
            "internal": True,
            "internal_type": "autonomous_wakeup",
            "tool_expiry_notice": NOTICE,
        },
        id="w1",
    )
    history = format_conversation_history(
        [wakeup, AIMessage(content="done", id="a1")],
        thread_id="t",
        show_autonomous_prompts=False,
        include_hidden_anchors=True,
    )
    assert [(e["role"], e.get("kind"), e.get("hidden")) for e in history] == [
        ("system", "tool_expiry_notice", None),
        ("user", None, True),
        ("assistant", None, None),
    ]
    assert history[0]["content"] == NOTICE and history[1]["message_id"] == "w1"


def test_strip_prompt_context_leaves_a_user_typed_system_line_alone():
    typed = "hello\n\n[System: I typed this myself]\n\nmore"
    assert strip_prompt_context(typed) == typed


def test_history_with_prompt_metadata_keeps_the_block_but_not_the_notice():
    history = format_conversation_history(
        [_prefixed_human()], thread_id="t", show_prompt_metadata=True
    )
    assert history[1]["role"] == "user"
    assert history[1]["content"] == f"{META}\n\nhello"


def test_history_mid_turn_notice_is_a_card_and_keeps_its_sub_turn_visible():
    prompt = make_pending_prompt(
        message=NOTICE,
        source="system",
        source_id=None,
        source_label="System notice",
        user_id="u",
        is_autonomous=True,
    )
    injected = build_queued_prompt_messages([prompt])[0]
    assert injected.additional_kwargs.get("internal_type") == "tool_expiry_notice"
    messages = [
        HumanMessage(content="do the task", id="h0"),
        *_tool_turn("a1", "c1"),
        injected,
        *_tool_turn("a2", "c2"),
        AIMessage(content="kit re-activated", id="a3"),
    ]
    history = format_conversation_history(
        messages, thread_id="t", show_autonomous_prompts=False, include_hidden_anchors=True
    )
    assert [(e["role"], e.get("kind")) for e in history] == [
        ("user", None),
        ("assistant", None),
        ("system", "tool_expiry_notice"),
        ("assistant", None),
    ]
    assert history[2]["content"] == NOTICE
    assert history[3]["content"] == "kit re-activated"
    assert not any(e.get("hidden") for e in history)
