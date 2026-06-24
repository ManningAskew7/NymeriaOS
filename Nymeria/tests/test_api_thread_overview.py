from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

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
        caller = self.thread_config_manager.get_config(caller_thread_id)
        if not caller or not caller.callable_team_id:
            return callables
        return [
            tc
            for tc in callables
            if tc.callable_team_id == caller.callable_team_id
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
