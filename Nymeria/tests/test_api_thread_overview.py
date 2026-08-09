from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.thread_config import (
    TemporaryToolEntry,
    ThreadConfig,
    ThreadConfigManager,
    ThreadLLMConfig,
)
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.core.time_utils import utc_now
from nymeria.core.todo_manager import TodoManager, TodoStatus
from nymeria.core.trigger_manager import TriggerAction, TriggerManager
from nymeria.core.user_profile import UserProfileManager
from nymeria.tools import SEED_TOOLS, CATALOG_TOOLS
from nymeria.tools.definitions.mcp_schema import MCPDiscoveredTool, MCPServerDefinition
from nymeria.vendor.react_agent.config import LLMConfig


class FakeThreadLocks:
    def __init__(self) -> None:
        self.lock_info = None

    def get_lock_info(self, thread_id: str):
        return self.lock_info


class FakeSkill:
    name = "kit-skill"
    description = "Skill Kit"
    scope = "user"
    allowed_tools = []
    required_tools = ["web_search"]
    tool_ttl = "30m"
    is_skill_kit = True
    has_scripts = False
    has_references = False
    has_assets = False


class FakeSkillManager:
    def list_installed(self, user_id: str):
        return [FakeSkill()]

    def list_for_thread(
        self,
        *,
        user_id: str,
        enabled_global_skills: list[str],
        thread_enabled_skills: list[str],
        thread_disabled_skills: list[str],
    ):
        return [FakeSkill()]


class FakeToolRegistry:
    def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": "mcp__srv__search"}]

    def get_tool(self, name: str):
        return None


class FakeAgent:
    MAIN_AGENT_MAX_ITERATIONS = 500
    CALLABLE_DEFAULT_MAX_ITERATIONS = 300

    def __init__(self, data_dir: Path):
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.profile_manager = UserProfileManager(data_dir)
        self.todo_manager = TodoManager(data_dir)
        self.trigger_manager = TriggerManager(data_dir)
        self.skill_manager = FakeSkillManager()
        self.tool_registry = FakeToolRegistry()
        self._thread_locks = FakeThreadLocks()
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1

    def get_context_stats(self, thread_id: str):
        return {
            "thread_id": thread_id,
            "model": "gpt-5.5",
            "total_tokens": 1200,
            "input_tokens": 1000,
            "output_tokens": 200,
            "cumulative_tokens": 2400,
            "context_limit": 128000,
            "usage_percentage": 0.9,
            "compaction_count": 1,
            "last_compaction": "2026-05-16T00:00:00+00:00",
            "context_management": "auto_compact",
        }

    def _get_llm_config_for_thread(self, thread_id: str = "") -> LLMConfig:
        return LLMConfig(
            provider="openai",
            model="gpt-5.5",
            api_key="configured",
            openai_api_mode="responses",
            reasoning_effort="high",
        )

    def _resolve_temporary_tools(self, tc: ThreadConfig) -> set[str]:
        now = utc_now()
        return {
            name
            for name, entry in tc.temporary_tools.items()
            if entry.expires_at > now
        }

    def _get_team_scoped_callable_threads(
        self,
        *,
        user_id: str,
        caller_thread_id: str,
    ):
        owned = set(self.accounts_repo.list_threads_for_user(user_id))
        callables = self.thread_config_manager.list_callable_threads(
            owned_thread_ids=owned,
        )
        # Mirrors the real bubble semantics (backlog #97): caller and target
        # teams must match, with "no team" itself a bubble.
        caller = self.thread_config_manager.get_config(caller_thread_id)
        caller_team = (caller.callable_team_id if caller else None) or None
        return [
            tc
            for tc in callables
            if (tc.callable_team_id or None) == caller_team
        ]


def _client(tmp_path: Path, api_client_builder):
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="owner",
        display_name="Owner",
    )
    agent.synced_tools = 0
    return client, agent, settings, token


def test_thread_overview_empty_default_config(tmp_path: Path, api_client_builder):
    client, agent, _settings, token = _client(tmp_path, api_client_builder)
    thread_id = "empty-thread"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    response = client.get(
        f"/threads/{thread_id}/overview",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["thread"]["thread_id"] == thread_id
    assert body["config_summary"]["has_customizations"] is False
    assert body["config_summary"]["instructions_present"] is False
    assert body["config_summary"]["system_prompt_override_present"] is False
    assert body["callable"]["enabled"] is False
    assert body["tools"]["disabled_names"] == []
    assert body["todos"]["total_count"] == 0
    assert body["triggers"]["total_count"] == 0
    assert body["chat_apps"]["binding_count"] == 0
    assert body["section_errors"] == {}


def test_thread_overview_tool_counts_use_defaults_plus_overrides(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, _settings, token = _client(tmp_path, api_client_builder)
    thread_id = "tool-counts"
    promoted_optional = next(iter(CATALOG_TOOLS))
    thread_extra = next(name for name in CATALOG_TOOLS if name != promoted_optional)
    default_core = SEED_TOOLS[0].name

    agent.accounts_repo.claim_thread(thread_id, "owner")
    with agent.profile_manager.atomic_update("owner") as profile:
        profile.tool_preferences.default_thread_tools = [
            default_core,
            promoted_optional,
        ]
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            enabled_tools=[promoted_optional, thread_extra],
            disabled_tools=[promoted_optional],
        )
    )

    response = client.get(
        f"/threads/{thread_id}/overview",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    tools = response.json()["tools"]
    assert tools["default_tool_names"] == sorted([default_core, promoted_optional])
    assert tools["enabled_optional_names"] == sorted(
        [promoted_optional, thread_extra]
    )
    assert tools["disabled_names"] == [promoted_optional]
    assert tools["effective_builtin_count"] == 2
    assert tools["total_effective_count"] == 2


def test_thread_overview_returns_resolved_sections(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, settings, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    thread_id = "thread-overview"
    helper_id = "helper-thread"
    now = utc_now()
    optional_tool = next(iter(CATALOG_TOOLS))

    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent.accounts_repo.claim_thread(helper_id, "owner")
    agent.thread_metadata_manager.upsert_thread(
        "owner",
        thread_id,
        title="Overview Thread",
        pinned=True,
        platform="desktop",
    )
    with agent.profile_manager.atomic_update("owner") as profile:
        profile.tool_preferences.default_thread_tools = [
            SEED_TOOLS[0].name,
            "mcp__srv__search",
        ]
        profile.enabled_global_skills = ["global-skill"]

    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            instructions="Use the ops checklist.",
            system_prompt="Custom system prompt.",
            enabled_tools=[optional_tool, "mcp__srv__search"],
            disabled_tools=["disabled_tool"],
            temporary_tools={
                optional_tool: TemporaryToolEntry(
                    enabled_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            },
            enabled_skills=["kit-skill"],
            disabled_skills=["disabled-skill"],
            llm_config=ThreadLLMConfig(
                provider="openai",
                model="gpt-5.5",
                api_key="do-not-return",
                openai_api_mode="responses",
            ),
            callable=True,
            callable_name="Analyst",
            callable_description="Analyze project state.",
            callable_max_iterations=9,
            callable_team_id="team-ops",
            callable_team_name="Ops",
            inject_profile_in_prompt=True,
            inject_todos_in_prompt=True,
            show_autonomous_prompts=True,
            show_prompt_metadata=True,
            telegram_autonomous_delivery="notify_only",
            in_app_notification_level="all_autonomous",
        )
    )
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=helper_id,
            callable=True,
            callable_name="Helper",
            callable_team_id="team-ops",
            callable_team_name="Ops",
        )
    )

    with sqlite3.connect(settings.db_path) as conn:
        conn.execute(
            "CREATE TABLE checkpoints ("
            "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
            "checkpoint BLOB, metadata BLOB)"
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?)",
            (thread_id, "", "0007", b"checkpoint", b"{}"),
        )
        conn.commit()

    mcp_dir = settings.data_dir / "mcp_servers"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "srv.json").write_text(
        MCPServerDefinition(
            id="srv",
            name="Search Server",
            server_command="python",
            enabled=True,
            discovered_tools=[
                MCPDiscoveredTool(name="search", description="Search")
            ],
        ).model_dump_json(),
        encoding="utf-8",
    )

    with agent.todo_manager.atomic_update("owner") as todos:
        scheduled = todos.add_item(
            "Scheduled task",
            scheduled_for=now + timedelta(minutes=30),
            thread_id=thread_id,
            created_by="user",
            recurrence="daily",
        )
        active = todos.add_item("Active task", thread_id=thread_id, created_by="user")
        done = todos.add_item("Done task", thread_id=thread_id, created_by="user")
        assert active is not None
        assert done is not None
        todos.update_item(active.id, status=TodoStatus.IN_PROGRESS)
        todos.update_item(done.id, status=TodoStatus.DONE)
        assert scheduled is not None

    trigger = agent.trigger_manager.add_trigger(
        user_id="owner",
        name="Morning brief",
        source_type="webhook",
        source_config={"secret": "test-secret"},
        action=TriggerAction(type="notify", config={}),
        thread_id=thread_id,
        enabled=True,
        created_by="user",
    )
    assert trigger is not None
    agent.trigger_manager.update_trigger(
        "owner",
        trigger.id,
        health_status="degraded",
        consecutive_errors=2,
        last_error="timeout",
    )

    agent.chat_bindings_repo.create_thread_binding(
        thread_id=thread_id,
        provider="telegram",
        platform_chat_id="123",
        user_id="owner",
    )
    agent._thread_locks.lock_info = {"owner": "test"}

    response = client.get(f"/threads/{thread_id}/overview", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["thread"]["title"] == "Analyst"
    assert body["thread"]["pinned"] is True
    assert body["status"] == {
        "thread_id": thread_id,
        "revision": "0007",
        "processing": True,
    }
    assert body["context"]["total_tokens"] == 1200
    assert body["config_summary"]["instructions_present"] is True
    assert body["config_summary"]["instructions_char_count"] == len(
        "Use the ops checklist."
    )
    assert body["config_summary"]["system_prompt_override_present"] is True
    assert body["config_summary"]["inject_profile_in_prompt"] is True
    assert body["llm"]["provider"] == "openai"
    assert body["llm"]["api_key_configured"] is True
    assert "api_key" not in body["llm"]["overrides"]
    assert body["llm"]["overrides"]["api_key_override_configured"] is True
    assert body["callable"]["enabled"] is True
    assert body["callable"]["effective_max_iterations"] == 9
    assert body["callable"]["visible_thread_count"] == 1
    assert body["callable"]["visible_threads"][0]["name"] == "Helper"
    assert optional_tool in body["tools"]["enabled_optional_names"]
    assert body["tools"]["live_temporary_tools"][0]["name"] == optional_tool
    assert body["tools"]["mcp_override_count"] == 1
    assert body["tools"]["effective_mcp_count"] == 1
    assert body["mcp"]["server_count"] == 1
    assert body["mcp"]["active_mcp_tool_count"] == 1
    assert body["skills"]["installed_count"] == 1
    assert body["skills"]["active_skill_kit_count"] == 1
    assert body["skills"]["required_tools"] == ["web_search"]
    assert body["todos"]["total_count"] == 3
    assert body["todos"]["active_count"] == 2
    assert body["todos"]["in_progress_count"] == 1
    assert body["todos"]["done_count"] == 1
    assert body["todos"]["scheduled_count"] == 1
    assert body["todos"]["recurring_count"] == 1
    assert body["todos"]["next_scheduled_item"]["task"] == "Scheduled task"
    assert body["triggers"]["total_count"] == 1
    assert body["triggers"]["degraded_count"] == 1
    assert body["triggers"]["triggers"][0]["last_error"] == "timeout"
    assert body["chat_apps"]["binding_count"] == 1
    assert body["chat_apps"]["providers"] == ["telegram"]
    assert body["chat_apps"]["telegram_delivery_mode"] == "notify_only"
    assert body["chat_apps"]["in_app_notification_level"] == "all_autonomous"
    assert body["user"] == {"id": "owner", "display_name": "Owner", "role": "user"}
    assert body["section_errors"] == {}


@pytest.mark.parametrize(
    ("provider", "base_url", "model", "expected_label"),
    [
        # Native google route at the proxy root = the antigravity channel.
        (
            "google",
            "http://cli-proxy-api:8317",
            "gemini-3.6-flash-high",
            "cliproxy Antigravity",
        ),
        # On the shared compat surface the model id names the channel;
        # gemini stays generic (either Google channel can serve it).
        (
            "openai",
            "http://localhost:8317/v1",
            "gemini-3.6-flash-high",
            "cliproxy Gemini",
        ),
        ("openai", "http://localhost:8317/v1", "gpt-5.5", "cliproxy Codex"),
        ("anthropic", "http://localhost:8317", "claude-fable-5", "cliproxy Claude"),
        # Direct-key google gets a proper label instead of the raw slot.
        ("google", None, "gemini-3-pro", "Gemini API"),
    ],
)
def test_thread_overview_provider_label_names_cliproxy_channel(
    tmp_path: Path,
    api_client_builder,
    provider: str,
    base_url: str | None,
    model: str,
    expected_label: str,
):
    client, agent, _settings, token = _client(tmp_path, api_client_builder)
    thread_id = "label-thread"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent._get_llm_config_for_thread = lambda thread_id="": LLMConfig(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key="configured",
    )

    response = client.get(
        f"/threads/{thread_id}/overview",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    assert response.json()["llm"]["provider_label"] == expected_label


@pytest.mark.parametrize(
    ("provider", "base_url", "api_mode", "expected"),
    [
        # google names its real wire; a stray "responses" mode on the config
        # must NOT leak into the label (#153: the pre-fix overview rendered
        # "responses" here while the CLI showed nothing).
        ("google", "http://cli-proxy-api:8317", "responses", "gemini/v1beta"),
        ("google", None, "responses", "gemini/v1beta"),
        ("openai", "http://localhost:8317/v1", "responses", "responses/v1"),
        ("anthropic", "http://localhost:8317", None, "messages/v1"),
    ],
)
def test_thread_overview_api_mode_label_mirrors_cli_header(
    tmp_path: Path,
    api_client_builder,
    provider: str,
    base_url: str | None,
    api_mode: str | None,
    expected: str,
):
    """_api_mode_label is the overview mirror of the CLI's _api_type_label;
    this pins the pair so they cannot silently diverge again (#153)."""
    client, agent, _settings, token = _client(tmp_path, api_client_builder)
    thread_id = "mode-label-thread"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent._get_llm_config_for_thread = lambda thread_id="": LLMConfig(
        provider=provider,
        model="any-model",
        base_url=base_url,
        api_key="configured",
        openai_api_mode=api_mode,
    )

    response = client.get(
        f"/threads/{thread_id}/overview",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    assert response.json()["llm"]["api_mode_label"] == expected


def test_thread_overview_llm_defaults_label_when_resolver_is_missing(
    tmp_path: Path,
    api_client_builder,
):
    """The degrade path (_llm_defaults) labels from global settings.

    It is live in production twice: as the fallback value when _llm_section
    raises, and inside _llm_section when the agent has no LLM-config
    resolver. Before this test the call site was entirely uncovered (a
    mutated label there left the whole overview suite green)."""
    client, agent, settings, token = _client(tmp_path, api_client_builder)
    thread_id = "defaults-thread"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent._get_llm_config_for_thread = None
    settings.llm_provider = "openai"
    settings.llm_base_url = "http://localhost:8317/v1"
    settings.llm_model = "gemini-3.6-flash-high"

    response = client.get(
        f"/threads/{thread_id}/overview",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    llm = response.json()["llm"]
    assert llm["provider_label"] == "cliproxy Gemini"
    assert llm["model"] == "gemini-3.6-flash-high"


@pytest.mark.parametrize(
    ("provider", "base_url", "model"),
    [
        ("anthropic", "http://localhost:8317", "claude-fable-5"),
        ("anthropic", "https://gateway.example.com", "claude-fable-5"),
        ("anthropic", None, "claude-fable-5"),
        ("openai", "http://cli-proxy-api:8317/v1", "gpt-5.5"),
        ("openai", "http://cli-proxy-api:8317/v1", "gemini-3.6-flash-high"),
        ("openai", "http://cli-proxy-api:8317/v1", "kimi-k2.5"),
        ("openai", "http://cli-proxy-api:8317/v1", "grok-4.3"),
        ("openai", "http://cli-proxy-api:8317/v1", "claude-fable-5"),
        ("openai", "http://cli-proxy-api:8317/v1", "gpt-oss-120b"),
        ("openai", "http://cli-proxy-api:8317/v1", "llama-3-70b"),
        ("openai", "https://api.example.com/v1", "gpt-5.5"),
        ("openai", None, "gpt-5.5"),
        ("google", "http://localhost:8318", "gemini-3.6-flash-high"),
        ("google", "https://generativelanguage.example.com", "gemini-3-pro"),
        ("google", None, "gemini-3-pro"),
        ("openrouter", "http://localhost:8317/v1", "gpt-5.5"),
        ("custom", "http://localhost:8317/v1", "some-model"),
        ("", None, ""),
        ("  anthropic  ", "http://localhost:8317", " CLAUDE-FABLE-5 "),
    ],
)
def test_provider_label_mirrors_agree(provider, base_url, model):
    """The two _provider_label bodies each claim sync in a docstring; this
    gates the claim so a drift in either mirror fails instead of shipping
    a divergent CLI-vs-GUI label."""
    from nymeria.api.thread_overview import _provider_label as overview_label
    from nymeria.triggers.cli.header import _provider_label as header_label

    assert overview_label(provider, base_url, model) == header_label(
        provider, base_url or "", model
    )


@pytest.mark.parametrize(
    ("provider", "mode"),
    [
        ("anthropic", None),
        ("google", None),
        ("google", "responses"),  # stray mode must not leak on either side
        ("openai", None),
        ("openai", "responses"),
        ("openai", "chat_completions"),
        ("openrouter", "chat_completions"),
        ("custom", "completions"),
        # Openai-compatible but outside both branch sets: BOTH sides blank
        # (the overview used to leak str(mode) here, the #153 class).
        ("deepseek", "chat_completions"),
        ("", None),
    ],
)
def test_api_mode_label_mirrors_cli_api_type_label(provider, mode):
    """Executable lockstep for the api-type pair, same idiom as the
    _provider_label gate above: both bodies are called on one table so a
    drift in either mirror fails instead of shipping CLI-vs-GUI skew."""
    from nymeria.api.thread_overview import _api_mode_label
    from nymeria.triggers.cli.header import _api_type_label

    assert _api_mode_label(provider, mode) == _api_type_label(
        provider, {}, {"openai_api_mode": mode}
    ), (provider, mode)


def test_thread_overview_enforces_thread_access(tmp_path: Path, api_client_builder):
    client, agent, _settings, owner_token = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user(
        "other",
        "other@example.com",
        "Other",
    )
    other_token = agent.accounts_repo.issue_token("other")
    thread_id = "private-thread"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    owner_response = client.get(
        f"/threads/{thread_id}/overview",
        headers=api_client_builder.auth(owner_token),
    )
    other_response = client.get(
        f"/threads/{thread_id}/overview",
        headers=api_client_builder.auth(other_token),
    )

    assert owner_response.status_code == 200
    assert other_response.status_code == 404


def test_live_temporary_tool_names_logs_unparseable_expiry(caplog):
    """A temporary-tool entry with a corrupt expiry is skipped with a debug log."""
    import logging
    from types import SimpleNamespace

    from nymeria.api.thread_overview import _live_temporary_tool_names

    class _BadEntry:
        @property
        def expires_at(self):
            raise ValueError("corrupt expiry")

    # No `_resolve_temporary_tools` attr -> the manual expiry-loop path runs.
    agent = SimpleNamespace()
    tc = SimpleNamespace(temporary_tools={"badtool": _BadEntry()})

    with caplog.at_level(logging.DEBUG, logger="nymeria.api.thread_overview"):
        result = _live_temporary_tool_names(agent, tc)

    assert result == set()
    assert "Skipping temporary tool badtool" in caplog.text
