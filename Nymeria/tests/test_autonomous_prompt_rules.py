"""Regression tests for autonomous prompt rules."""

from __future__ import annotations

from datetime import datetime, timezone

from nymeria.core.agent import NymeriaAgent
from nymeria.core.prompts import AUTONOMOUS_MODE_RULES
from nymeria.core.thread_config import ThreadConfig
from nymeria.core.todo_manager import TodoManager
from nymeria.triggers.watchdog_worker import WatchdogWorker


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


def test_default_prompt_is_source_invariant():
    agent = _agent_with_configs()

    autonomous = agent._build_full_system_prompt(
        user_id="default", thread_id="thread-1", is_autonomous=True
    )
    interactive = agent._build_full_system_prompt(
        user_id="default", thread_id="thread-1", is_autonomous=False
    )

    # The system prompt must not change based on turn source.
    assert autonomous == interactive
    assert "BASE PROMPT" in autonomous
    assert "## Autonomous Run Rules" not in autonomous


def test_regular_custom_prompt_is_source_invariant():
    config = ThreadConfig(thread_id="thread-1", system_prompt="CUSTOM PROMPT")
    agent = _agent_with_configs({"thread-1": config})

    autonomous = agent._build_full_system_prompt(
        user_id="default", thread_id="thread-1", is_autonomous=True
    )
    interactive = agent._build_full_system_prompt(
        user_id="default", thread_id="thread-1", is_autonomous=False
    )

    assert autonomous == interactive
    assert "CUSTOM PROMPT" in autonomous
    assert "BASE PROMPT" not in autonomous
    assert "## Autonomous Run Rules" not in autonomous


def test_callable_custom_prompt_is_source_invariant():
    config = ThreadConfig(
        thread_id="callable-1",
        callable=True,
        callable_name="HelperAgent",
        system_prompt="CALLABLE PROMPT",
        inject_todos_in_prompt=True,
        inject_profile_in_prompt=True,
    )
    agent = _agent_with_configs({"callable-1": config})

    autonomous = agent._build_full_system_prompt(
        user_id="default", thread_id="callable-1", is_autonomous=True
    )
    interactive = agent._build_full_system_prompt(
        user_id="default", thread_id="callable-1", is_autonomous=False
    )

    assert autonomous == interactive
    assert "CALLABLE PROMPT" in autonomous
    # Focused context: no soul.md, no profile/TODO injection.
    assert "BASE PROMPT" not in autonomous
    assert "## Active TODOs" not in autonomous
    assert "## User Profile" not in autonomous
    # No mode rules and no embedded time/source: those live in the tail metadata.
    assert "## Autonomous Run Rules" not in autonomous
    assert "[Time:" not in autonomous
    assert "[Trigger:" not in autonomous


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
    worker = WatchdogWorker.__new__(WatchdogWorker)
    worker.staleness_minutes = 20

    message = worker._build_nudge_message(
        [
            {
                "id": "abc12345",
                "task": "Check stale task",
                "status": "pending",
                "updated_at": "2026-04-27T00:00:00Z",
            }
        ]
    )

    assert "nym_todo tool" in message
    assert "nym_todo_delete" in message
    assert "remain silent" not in message.lower()
    assert "stay silent" not in message.lower()
