"""Isolated unit tests for the agent_prompt cluster (Workstream B).

These exercise the extracted prompt/memory functions against a narrow,
fully typed ``PromptHost`` stub built WITHOUT a ``NymeriaAgent`` (and with no
``cast(Any, ...)``): the variable is declared as the Protocol, so pyrefly must
accept a plain non-agent host. That constructibility is the point of giving
the module a narrow typed input.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

from nymeria.core.agent_prompt import (
    PromptHost,
    build_active_todos_section,
    build_full_system_prompt,
    build_user_profile_section,
    get_time_context_for_agent,
)


class _PromptHost:
    """Minimal, fully typed PromptHost stub (no NymeriaAgent)."""

    def __init__(
        self,
        *,
        profile: Any = None,
        thread_config: Any = None,
        base_system_prompt: str = "SOUL",
        todos: Any = None,
    ) -> None:
        self._profile = profile
        self._thread_config = thread_config
        self._base_system_prompt = base_system_prompt
        self._todos = todos
        self.profile_manager = SimpleNamespace(get_profile=lambda _uid: self._profile)
        self.todo_manager = SimpleNamespace(get_todos=lambda _uid: self._todos)
        self.thread_config_manager = SimpleNamespace(
            get_config=lambda _tid: self._thread_config
        )
        self.settings = SimpleNamespace(data_dir=None)
        self._memory_indexes: dict = {}

    def _resolve_temporary_tools(self, tc: Any) -> set:
        return set()

    def _skills_fingerprint(self, user_id: str, thread_id: str) -> str:
        return ""

    def _build_user_profile_section(self, user_id: str) -> str:
        return build_user_profile_section(self, user_id)

    def _build_active_todos_section(self, user_id: str, thread_id: str = "") -> str:
        return build_active_todos_section(self, user_id, thread_id)

    def _get_memory_index(self, user_id: str) -> Any:
        return None


def _profile(*, memories=None, overrides=None) -> Any:
    return SimpleNamespace(
        memories=memories or [],
        personality_overrides=overrides or {},
    )


def _thread_config(**overrides: Any) -> Any:
    base = dict(
        callable=False,
        system_prompt="",
        inject_profile_in_prompt=False,
        inject_todos_in_prompt=False,
        instructions="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_prompt_host_is_runtime_checkable():
    host = _PromptHost(profile=_profile())
    assert isinstance(host, PromptHost)


def test_build_user_profile_section_renders_facts_and_prefs():
    profile = _profile(
        memories=[SimpleNamespace(key="city", value="Sydney")],
        overrides={"tone": "concise"},
    )
    # No cast: declared as the narrow Protocol.
    host: PromptHost = _PromptHost(profile=profile)

    section = build_user_profile_section(host, "u1")

    assert "## User Profile" in section
    assert "city" in section and "Sydney" in section
    assert "tone" in section and "concise" in section


def test_build_user_profile_section_empty_when_no_profile_data():
    host: PromptHost = _PromptHost(profile=_profile())
    assert build_user_profile_section(host, "u1") == ""


def test_build_full_system_prompt_returns_base_without_thread_config():
    host: PromptHost = _PromptHost(base_system_prompt="SOUL-BASE")
    # No thread_id -> no thread config -> plain base prompt.
    assert build_full_system_prompt(host, "u1", "") == "SOUL-BASE"


def test_build_full_system_prompt_appends_thread_instructions():
    tc = _thread_config(instructions="Be terse.")
    host: PromptHost = _PromptHost(
        base_system_prompt="SOUL-BASE", thread_config=tc
    )

    prompt = build_full_system_prompt(host, "u1", "thread-1")

    assert prompt.startswith("SOUL-BASE")
    assert "## Thread-Specific Instructions" in prompt
    assert "Be terse." in prompt


def test_get_time_context_for_agent_is_a_pure_host_free_leaf():
    # Takes no host at all; both call shapes return a non-empty string.
    interactive: Optional[str] = get_time_context_for_agent()
    autonomous: Optional[str] = get_time_context_for_agent(is_autonomous=True)

    assert isinstance(interactive, str) and interactive
    assert isinstance(autonomous, str) and autonomous
