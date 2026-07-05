"""Regression tests for autonomous prompt rules."""

from __future__ import annotations

from datetime import datetime, timezone

from nymeria.core.agent import NymeriaAgent
from nymeria.core.prompts import AUTONOMOUS_MODE_RULES, get_autonomous_tail_guidance
from nymeria.core.thread_agent_executor import _format_handoff_prompt
from nymeria.core.thread_config import ThreadConfig
from nymeria.core.todo_manager import TodoItem, TodoManager
from nymeria.core.watchdog_sweep import WatchdogSweep


class FakeThreadConfigManager:
    def __init__(self, configs: dict[str, ThreadConfig] | None = None):
        self.configs = configs or {}

    def get_config(self, thread_id: str) -> ThreadConfig | None:
        return self.configs.get(thread_id)


def _agent_with_configs(configs: dict[str, ThreadConfig] | None = None) -> NymeriaAgent:
    agent = NymeriaAgent.__new__(NymeriaAgent)
    agent.thread_config_manager = FakeThreadConfigManager(configs)
    agent._base_system_prompt = "BASE PROMPT"
    return agent


def test_autonomous_rules_are_explicitly_non_silent():
    assert AUTONOMOUS_MODE_RULES.strip()
    assert "Do not end by choosing silence" in AUTONOMOUS_MODE_RULES
    assert "nothing to do" in AUTONOMOUS_MODE_RULES
    assert "nym_todo" in AUTONOMOUS_MODE_RULES


# The general autonomous guidance now rides on the message tail (not the system
# prompt). get_autonomous_tail_guidance() gates it on the turn being autonomous.


def test_tail_guidance_empty_for_interactive_turns():
    assert get_autonomous_tail_guidance(False) == ""


def test_tail_guidance_carries_non_silence_rules_for_autonomous_turns():
    guidance = get_autonomous_tail_guidance(True)
    assert "## Autonomous Run Rules" in guidance
    assert "Do not end by choosing silence" in guidance
    assert "nym_todo" in guidance
    # Tail-friendly: no leading horizontal-rule separator (that was for the old
    # system-prompt append).
    assert not guidance.startswith("---")


def test_prefix_turn_metadata_appends_guidance_only_when_autonomous(monkeypatch):
    agent = _agent_with_configs()
    monkeypatch.setattr(
        agent, "_get_time_context", lambda **kwargs: "[Time: X]\n[Trigger: Y]"
    )

    autonomous = agent._prefix_turn_metadata(
        "do the thing", is_self_invoke=True, trigger_override=None, is_autonomous=True
    )
    assert autonomous.startswith("[Time: X]\n[Trigger: Y]")
    assert "## Autonomous Run Rules" in autonomous
    assert autonomous.endswith("do the thing")

    interactive = agent._prefix_turn_metadata(
        "say hi", is_self_invoke=False, trigger_override=None, is_autonomous=False
    )
    assert interactive == "[Time: X]\n[Trigger: Y]\n\nsay hi"
    assert "Autonomous Run Rules" not in interactive


def test_handoff_metadata_block_explains_non_return_and_callback():
    prompt = _format_handoff_prompt(
        task="summarize the report",
        handoff_id="handoff-abc123",
        caller_thread_id="thread-42",
        caller_name="Planner",
        callable_name="Researcher",
    )
    # Routing facts the receiver needs to call the source back.
    assert "[Handoff Metadata]" in prompt
    assert "handoff_id: handoff-abc123" in prompt
    assert "source_thread_id: thread-42" in prompt
    assert "source_thread_name: Planner" in prompt
    # Behavioral guidance: output is not returned; call back if callable; else notify.
    assert "non-blocking handoff" in prompt
    assert "NOT returned" in prompt
    assert "call it back" in prompt
    assert "notify tool" in prompt
    # The actual task still trails the metadata block.
    assert prompt.rstrip().endswith("summarize the report")


# The system prompt no longer takes a turn-source argument, so it is structurally
# source-invariant. These tests guard that no mode rules and no embedded time/
# trigger metadata leak into it (that signal lives in the message tail instead),
# and that the build is deterministic (no timestamp creeps back in).


def test_default_prompt_omits_mode_rules_and_time():
    agent = _agent_with_configs()

    prompt = agent._build_full_system_prompt(user_id="default", thread_id="thread-1")

    assert "BASE PROMPT" in prompt
    assert "## Autonomous Run Rules" not in prompt
    assert "[Time:" not in prompt
    assert "[Trigger:" not in prompt
    # Deterministic: rebuilding yields the identical prefix.
    assert prompt == agent._build_full_system_prompt(
        user_id="default", thread_id="thread-1"
    )


def test_regular_custom_prompt_omits_mode_rules_and_time():
    config = ThreadConfig(thread_id="thread-1", system_prompt="CUSTOM PROMPT")
    agent = _agent_with_configs({"thread-1": config})

    prompt = agent._build_full_system_prompt(user_id="default", thread_id="thread-1")

    assert "CUSTOM PROMPT" in prompt
    assert "BASE PROMPT" not in prompt
    assert "## Autonomous Run Rules" not in prompt
    assert "[Time:" not in prompt
    assert "[Trigger:" not in prompt


def test_callable_custom_prompt_is_focused_and_omits_mode_rules_and_time():
    config = ThreadConfig(
        thread_id="callable-1",
        callable=True,
        callable_name="HelperAgent",
        system_prompt="CALLABLE PROMPT",
        inject_todos_in_prompt=True,
        inject_profile_in_prompt=True,
    )
    agent = _agent_with_configs({"callable-1": config})

    prompt = agent._build_full_system_prompt(user_id="default", thread_id="callable-1")

    assert "CALLABLE PROMPT" in prompt
    # Focused context: no soul.md, no profile/TODO injection.
    assert "BASE PROMPT" not in prompt
    assert "## Active TODOs" not in prompt
    assert "## User Profile" not in prompt
    # No mode rules and no embedded time/source: those live in the tail metadata.
    assert "## Autonomous Run Rules" not in prompt
    assert "[Time:" not in prompt
    assert "[Trigger:" not in prompt


def test_active_todos_section_uses_current_tool_names(tmp_path):
    agent = _agent_with_configs()
    agent.todo_manager = TodoManager(tmp_path)

    with agent.todo_manager.atomic_update("default") as todo_list:
        todo_list.add_item(
            "Review scheduled prompt behavior",
            scheduled_for=datetime.now(timezone.utc),
            thread_id="thread-1",
        )

    section = agent._build_active_todos_section("default", "thread-1")

    assert "nym_todo(todo_id=..., status='done')" in section
    assert "nym_todo_delete" in section
    assert "Use todo(todo_id=..., status='done')" not in section
    assert "Only todo_delete" not in section


def test_watchdog_nudge_uses_current_tool_names_and_no_silence_language():
    sweep = WatchdogSweep.__new__(WatchdogSweep)
    sweep.staleness_minutes = 20

    message = sweep._build_nudge_message(
        [
            TodoItem(
                id="abc12345",
                task="Check stale task",
                thread_id="thread-1",
                updated_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
            )
        ]
    )

    assert "nym_todo tool" in message
    assert "nym_todo_delete" in message
    assert "remain silent" not in message.lower()
    assert "stay silent" not in message.lower()
