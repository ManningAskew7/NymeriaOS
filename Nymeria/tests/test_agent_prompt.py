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
    get_memory_hash,
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
        role: str = "user",
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
        # `get_memory_hash` folds the owner's role into the cache-freshness
        # token (#327), so the host surface includes the account lookup.
        self.accounts_repo = SimpleNamespace(
            get_user_by_id=lambda _uid: SimpleNamespace(role=role)
        )
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


def _hash_host(*, role: str) -> Any:
    """A host whose profile carries the `tool_preferences` the hash reads.

    The shared `_profile()` helper omits it: nothing in this file exercised
    `get_memory_hash` before #327.
    """
    profile = SimpleNamespace(
        memories=[],
        personality_overrides={},
        tool_preferences=SimpleNamespace(default_thread_tools=None),
    )
    return _PromptHost(profile=profile, role=role)


def test_get_memory_hash_tracks_the_owner_role():
    """#327: role is a cache-freshness input, proved on the fully typed host.

    The graph cache checks this hash before it will rebuild, and only a rebuild
    re-runs the role gate that strips admin-only tools. If two accounts that
    differ ONLY in role hash the same, a demotion is served the graph compiled
    while the account was still an admin.
    """
    as_admin = get_memory_hash(_hash_host(role="admin"), "u1", "")
    as_user = get_memory_hash(_hash_host(role="user"), "u1", "")

    assert as_admin != as_user


def test_get_memory_hash_is_stable_for_an_unchanged_role():
    # The over-correction guard: a term that varied per call would evict on
    # every lookup and turn the cache off rather than keep it honest.
    first = get_memory_hash(_hash_host(role="admin"), "u1", "")
    second = get_memory_hash(_hash_host(role="admin"), "u1", "")

    assert first == second


# #164: the agent learns which standard tools shipped after setup
# A real UserProfile and ThreadConfig on the typed stub host: the status rule
# and the thread lists are the inputs under test, so neither is faked.

HINT_HEADING = "## Standard Tools Not Enabled"


def _newest_seed_tool() -> str:
    from nymeria.tools import SEED_TOOL_PROMOTED, core_seed_tool_names

    return max(core_seed_tool_names(), key=lambda name: SEED_TOOL_PROMOTED[name])


def _discovery_profile(*, set_up_before: bool = True, declined=(), lacking=None) -> Any:
    from datetime import datetime, timedelta, timezone

    from nymeria.core.user_profile import ToolPreferences, UserProfile
    from nymeria.tools import SEED_TOOL_PROMOTED, fresh_default_thread_tool_names

    newest = _newest_seed_tool()
    missing = {newest} if lacking is None else set(lacking)
    promoted = datetime.combine(
        SEED_TOOL_PROMOTED[newest], datetime.min.time(), timezone.utc
    )
    return UserProfile(
        user_id="u1",
        created_at=promoted - timedelta(days=1) if set_up_before else datetime.now(timezone.utc),
        tool_preferences=ToolPreferences(
            default_thread_tools=[
                n for n in fresh_default_thread_tool_names() if n not in missing
            ],
            declined_core_tools=list(declined),
        ),
    )


class _TempToolsHost(_PromptHost):
    """A host whose thread has live TTL'd tools (a kit bind)."""

    def __init__(self, *, temporary: set, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._temporary = temporary

    def _resolve_temporary_tools(self, tc: Any) -> set:
        return set(self._temporary)


def _real_thread_config(**overrides: Any) -> Any:
    from nymeria.core.thread_config import ThreadConfig

    return ThreadConfig(thread_id="t1", **overrides)


def test_the_prompt_names_a_new_standard_tool_and_how_the_user_enables_it():
    """E2: the agent is told the tool exists, so it stops answering "I do not
    have that capability", and it only informs (N6)."""
    newest = _newest_seed_tool()
    host: PromptHost = _PromptHost(
        profile=_discovery_profile(),
        thread_config=_real_thread_config(instructions="Be terse."),
        base_system_prompt="SOUL-BASE",
    )

    prompt = build_full_system_prompt(host, "u1", "t1")

    assert HINT_HEADING in prompt
    hint = prompt[prompt.index(HINT_HEADING):prompt.index("## Thread-Specific Instructions")]
    assert f"- {newest}: " in hint
    assert "`/tools enable <name>`" in hint and "global" in hint
    assert "do not say the capability does not exist" in hint
    assert "Do not\nenable it yourself." in hint
    # After the base prompt, before the thread's own instructions.
    assert prompt.startswith("SOUL-BASE")


def test_a_fresh_account_gets_no_hint():
    """E1: nothing is new, so the section (and its tokens) is absent."""
    host: PromptHost = _PromptHost(
        profile=_discovery_profile(set_up_before=False, lacking=()),
        thread_config=_real_thread_config(),
        base_system_prompt="SOUL-BASE",
    )

    assert build_full_system_prompt(host, "u1", "t1") == "SOUL-BASE"


def test_an_account_set_up_after_the_promotion_gets_no_hint():
    """E6: lacking a tool it was offered is a removal, not news."""
    host: PromptHost = _PromptHost(
        profile=_discovery_profile(set_up_before=False),
        thread_config=_real_thread_config(),
    )

    assert HINT_HEADING not in build_full_system_prompt(host, "u1", "t1")


def test_a_declined_tool_is_never_hinted():
    """E4: the decline record silences the hint."""
    host: PromptHost = _PromptHost(
        profile=_discovery_profile(declined=[_newest_seed_tool()]),
        thread_config=_real_thread_config(),
    )

    assert HINT_HEADING not in build_full_system_prompt(host, "u1", "t1")


def test_a_thread_that_binds_or_disables_the_tool_gets_no_hint():
    """E3: bound here (enabled or a live TTL entry) needs no hint; disabled
    here was a deliberate choice for this thread."""
    newest = _newest_seed_tool()
    profile = _discovery_profile()
    enabled: PromptHost = _PromptHost(
        profile=profile, thread_config=_real_thread_config(enabled_tools=[newest])
    )
    disabled: PromptHost = _PromptHost(
        profile=profile, thread_config=_real_thread_config(disabled_tools=[newest])
    )
    temporary: PromptHost = _TempToolsHost(
        profile=profile, thread_config=_real_thread_config(), temporary={newest}
    )
    unrelated: PromptHost = _PromptHost(
        profile=profile, thread_config=_real_thread_config(enabled_tools=["web_search_brave"])
    )

    assert HINT_HEADING not in build_full_system_prompt(enabled, "u1", "t1")
    assert HINT_HEADING not in build_full_system_prompt(disabled, "u1", "t1")
    assert HINT_HEADING not in build_full_system_prompt(temporary, "u1", "t1")
    assert HINT_HEADING in build_full_system_prompt(unrelated, "u1", "t1")


def test_callable_threads_with_their_own_prompt_and_dream_threads_get_no_hint():
    profile = _discovery_profile()
    callable_host: PromptHost = _PromptHost(
        profile=profile,
        thread_config=_real_thread_config(
            callable=True, callable_name="helper", system_prompt="FOCUSED"
        ),
    )
    dream_host: PromptHost = _PromptHost(
        profile=profile,
        thread_config=_real_thread_config(system_prompt="DREAM", shadow_parent_id="t0"),
    )

    assert build_full_system_prompt(callable_host, "u1", "t1") == "FOCUSED"
    assert build_full_system_prompt(dream_host, "u1", "t1") == "DREAM"


def test_dismissing_the_tool_changes_the_graph_cache_hash():
    """The hint is part of the cached prompt, so a Dismiss (decline record
    only, the defaults list unchanged) must rebuild it once; an unchanged
    account must keep hitting the cache."""
    newest = _newest_seed_tool()
    tc = _real_thread_config()
    offered = get_memory_hash(_PromptHost(profile=_discovery_profile(), thread_config=tc), "u1", "t1")
    again = get_memory_hash(_PromptHost(profile=_discovery_profile(), thread_config=tc), "u1", "t1")
    dismissed = get_memory_hash(
        _PromptHost(profile=_discovery_profile(declined=[newest]), thread_config=tc),
        "u1",
        "t1",
    )

    assert offered == again
    assert offered != dismissed
